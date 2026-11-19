"""Scoped, resumable multipart uploads. Only the server can seal or admit an object."""
import hashlib
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func

from .auth import Access, aware, now, get_access
from .db import get_db, SessionLocal
from .document_ingestion import admit, check_format, MAX_DOCUMENT_BYTES, SUFFIXES
from .models import Document, DocumentStatus, DocumentUpload, IngestionJob, JobState, Membership, User
from .storage_backend import current_storage

router = APIRouter(tags=["document-uploads"])
PART_SIZE = 8 * 1024 * 1024


class UploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=150)
    size_bytes: int = Field(gt=0, le=MAX_DOCUMENT_BYTES, strict=True)
    version_of: UUID | None = None


class PartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def view(row, parts=None):
    return {"id": str(row.id), "knowledge_base_id": str(row.knowledge_base_id), "filename": row.filename,
        "size_bytes": row.size_bytes, "part_size": PART_SIZE, "state": row.state, "error": row.error,
        "expires_at": row.expires_at.isoformat(), "parts": parts or [],
        "document_id": str(row.document_id) if row.document_id else None}


def owned(db, access, identifier):
    row = db.get(DocumentUpload, identifier, with_for_update=True, populate_existing=True)
    if not row or row.user_id != access.user_id or row.workspace_id != access.workspace_id:
        raise HTTPException(404, "上传任务不存在")
    access.library(db, row.knowledge_base_id, write=True)
    if aware(row.expires_at) <= now() and row.state in ("uploading", "completing"):
        raise HTTPException(410, "上传已过期，请重新选择文件")
    return row


def cloud_parts(storage, row):
    parts = storage.parts(row.object_key, row.multipart_id)
    if isinstance(parts, dict):
        parts = [parts]
    return sorted(parts, key=lambda part: int(part["PartNumber"]))


@router.post("/knowledge-bases/{library_id}/uploads", status_code=201)
def create_upload(library_id: UUID, payload: UploadInput, db=Depends(get_db), access=Depends(get_access)):
    access.library(db, library_id, write=True)
    from .storage_billing import check_capacity
    check_capacity(db, access.workspace_id, payload.size_bytes)
    if (Path(payload.filename).suffix.lower() not in SUFFIXES or "/" in payload.filename
            or "\\" in payload.filename or any(ord(c) < 32 for c in payload.filename + payload.content_type)):
        raise HTTPException(415, "不支持的文件名或文档格式")
    if payload.version_of:
        previous = db.get(Document, payload.version_of)
        if not previous or previous.knowledge_base_id != library_id or previous.status == DocumentStatus.DELETED:
            raise HTTPException(404, "原文档版本不存在")
    if db.scalar(select(func.count()).select_from(DocumentUpload).where(
            DocumentUpload.user_id == access.user_id, DocumentUpload.state.in_(["uploading", "completing", "verifying"]),
            DocumentUpload.expires_at > now())) >= 20:
        raise HTTPException(429, "进行中的上传过多，请先完成或取消部分任务")
    storage = current_storage(db)
    identifier = uuid4()
    key = f"{library_id}/{identifier}{Path(payload.filename).suffix.lower()}"
    multipart_id = storage.begin(key, payload.content_type)
    row = DocumentUpload(id=identifier, user_id=access.user_id, workspace_id=access.workspace_id,
        knowledge_base_id=library_id, filename=payload.filename, content_type=payload.content_type,
        size_bytes=payload.size_bytes, version_of=payload.version_of, object_key=key,
        multipart_id=multipart_id, state="uploading", expires_at=now()+timedelta(hours=24))
    db.add(row)
    try:
        db.commit()
    except Exception:
        storage.abort(key, multipart_id)
        raise
    return view(row)


@router.get("/uploads/{identifier}")
def read_upload(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    parts = cloud_parts(current_storage(db), row) if row.state == "uploading" else []
    result = view(row, [{"number": int(part["PartNumber"]), "size": int(part["Size"]),
        "sha256": row.parts.get(str(part["PartNumber"]))} for part in parts])
    if row.document_id:
        document = db.get(Document, row.document_id, populate_existing=True)
        if document:
            result["document_status"] = document.status.value
            result["document_error"] = document.error_detail
            if document.status == DocumentStatus.PROCESSING:
                job = db.scalar(
                    select(IngestionJob)
                    .where(IngestionJob.document_id == document.id, IngestionJob.state == JobState.RUNNING,
                           IngestionJob.retry_progress.is_not(None))
                    .order_by(IngestionJob.created_at.desc())
                    .limit(1)
                )
                result["retry_progress"] = job.retry_progress if job else None
    return result


@router.post("/uploads/{identifier}/parts/{number}")
def sign_part(identifier: UUID, number: int, payload: PartInput, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    if row.state != "uploading":
        raise HTTPException(409, "此上传任务不能继续传输")
    if number < 1 or number > math.ceil(row.size_bytes / PART_SIZE):
        raise HTTPException(422, "分片编号不正确")
    old = row.parts.get(str(number))
    if old and old != payload.sha256:
        raise HTTPException(409, "文件内容与原上传不一致，请重新发起上传")
    size = min(PART_SIZE, row.size_bytes - (number-1)*PART_SIZE)
    row.parts = {**row.parts, str(number): payload.sha256}
    url = current_storage(db).part_url(row.object_key, row.multipart_id, number, size)
    db.commit()
    return {"url": url, "expires_in": 900, "size": size}


@router.post("/uploads/{identifier}/complete", status_code=202)
def complete_upload(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    if row.state in ("completed", "verifying"):
        if row.state == "verifying":
            from .tasks import verify_upload
            verify_upload.delay(str(row.id))
        return view(row)
    if row.state not in ("uploading", "completing"):
        raise HTTPException(409, "上传任务已经结束，请重新上传")
    storage = current_storage(db)
    if row.state == "uploading":
        parts = cloud_parts(storage, row)
        if len(parts) != math.ceil(row.size_bytes / PART_SIZE):
            raise HTTPException(409, "仍有分片未上传完成")
        for index, part in enumerate(parts, 1):
            expected = min(PART_SIZE, row.size_bytes - (index-1)*PART_SIZE)
            if int(part["PartNumber"]) != index or int(part["Size"]) != expected or str(index) not in row.parts:
                raise HTTPException(409, "分片校验失败，请重新上传")
        row.state = "completing"
        db.commit()
    # A crash after COS completion is recovered using the sealed object's HEAD.
    row = owned(db, access, identifier)
    if row.state != "completing":
        return view(row)
    head = storage.head(row.object_key)
    if head is None:
        parts = cloud_parts(storage, row)
        storage.complete(row.object_key, row.multipart_id,
            [{"PartNumber": int(part["PartNumber"]), "ETag": part["ETag"]} for part in parts])
        head = storage.head(row.object_key)
    if int(head["Content-Length"]) != row.size_bytes:
        raise HTTPException(409, "文件实际大小与声明不符")
    row.state = "verifying"
    db.commit()
    from .tasks import verify_upload
    verify_upload.delay(str(row.id))
    return view(row)


@router.delete("/uploads/{identifier}", status_code=204)
def cancel_upload(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    if row.state == "completed":
        raise HTTPException(409, "文件已入库，请在文档列表中管理")
    storage = current_storage(db)
    if row.state in ("uploading", "completing"):
        storage.abort(row.object_key, row.multipart_id)
    storage.delete(row.object_key)
    row.state = "cancelled"
    db.commit()


def finalize_upload(identifier):
    from .tasks import process_document
    with SessionLocal() as db:
        row = db.get(DocumentUpload, UUID(identifier), with_for_update=True, populate_existing=True)
        if not row or row.state != "verifying":
            return
        storage = current_storage(db)
        try:
            access = worker_access(db, row)
            access.library(db, row.knowledge_base_id, write=True)
            with storage.download(row.object_key) as stream:
                from .main import inspect_upload
                size, digest = inspect_upload(stream)
                if size != row.size_bytes:
                    raise HTTPException(409, "文件实际大小不一致")
                check_format(stream, row.filename)
                content = stream.read()
            # Verify the actual content against all browser part fingerprints.
            for number in range(1, math.ceil(size / PART_SIZE)+1):
                chunk = content[(number-1)*PART_SIZE:number*PART_SIZE]
                if hashlib.sha256(chunk).hexdigest() != row.parts.get(str(number)):
                    raise HTTPException(409, "文件摘要校验失败，请重新上传")
            access = worker_access(db, row)
            document, job = admit(db, access, row.knowledge_base_id, row.filename, row.content_type,
                size, digest, row.object_key, row.version_of)
            row.document_id, row.state = document.id, "completed"
            job_id = str(job.id)
            db.commit()
        except Exception as error:
            db.rollback()
            row = db.get(DocumentUpload, UUID(identifier), with_for_update=True, populate_existing=True)
            if row and row.state == "verifying":
                row.state, row.error = "failed", error.detail if isinstance(error, HTTPException) else "文件校验失败，请重试上传"
                db.commit()
                try:
                    storage.delete(row.object_key)
                except Exception:
                    pass
            return
    process_document.run(job_id, prepared_content=content)


def worker_access(db, row):
    user = db.get(User, row.user_id, populate_existing=True, with_for_update=True)
    member = db.get(Membership, (row.workspace_id, row.user_id), populate_existing=True, with_for_update=True)
    if not user or user.is_suspended or not member:
        raise HTTPException(403, "上传权限已失效")
    return Access(row.user_id, row.workspace_id, member.role, "")


def cleanup_uploads():
    with SessionLocal() as db:
        rows = db.scalars(select(DocumentUpload).where(DocumentUpload.expires_at <= now(),
            DocumentUpload.state.in_(["uploading", "completing", "failed", "cancelled", "expired"]))
            .with_for_update(skip_locked=True)).all()
        if not rows:
            return
        storage = current_storage(db)
        for row in rows:
            try:
                if row.state in ("uploading", "completing"):
                    try:
                        storage.abort(row.object_key, row.multipart_id)
                    except HTTPException:
                        pass
                storage.delete(row.object_key)
                row.state = "cleaned"
                db.commit()
            except Exception:
                db.rollback()


def recover_uploads():
    from .tasks import verify_upload, process_document
    from .models import IngestionJob, JobState
    with SessionLocal() as db:
        for identifier in db.scalars(select(DocumentUpload.id).where(DocumentUpload.state == "verifying")):
            verify_upload.delay(str(identifier))
        # Admission can commit just before the verifier stops; recover only unstarted work.
        for job in db.scalars(select(IngestionJob).join(DocumentUpload,
                DocumentUpload.document_id == IngestionJob.document_id).where(
                    DocumentUpload.state == "completed", IngestionJob.state == JobState.QUEUED)):
            process_document.delay(str(job.id))


def cleanup_orphans():
    """Only platform-shaped objects older than the session lifetime are eligible."""
    from .storage_backend import active_settings
    with SessionLocal() as db:
        settings = active_settings(db)
        if not settings or not settings.active:
            return
        storage = current_storage(db)
        cutoff = now() - timedelta(hours=24)
        managed = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}\.[a-z0-9]+$")
        def old(value):
            try:
                return aware(datetime.fromisoformat(value.replace("Z", "+00:00"))) < cutoff
            except (ValueError, TypeError, AttributeError):
                return False
        for candidate in storage.incomplete_uploads():
            key = candidate["Key"]
            if managed.fullmatch(key) and old(candidate.get("Initiated")) and not db.scalar(
                    select(DocumentUpload.id).where(DocumentUpload.object_key == key,
                        DocumentUpload.state.in_(["uploading", "completing", "verifying"]))):
                storage.abort(key, candidate["UploadId"])
        for candidate in storage.iter_objects():
            key = candidate["Key"]
            if not old(candidate.get("LastModified")):
                continue
            if key.startswith(("_ocr/", "_checks/")):
                storage.delete(key)
            elif re.fullmatch(r"_chat/[0-9a-f-]{36}/[0-9a-f-]{36}\.[a-z0-9]+", key):
                from .models import ChatAttachment
                if not db.scalar(select(ChatAttachment.id).where(ChatAttachment.object_key == key)):
                    storage.delete(key)
            elif managed.fullmatch(key):
                # Include soft-deleted originals: restore and references must remain valid.
                if not db.scalar(select(Document.id).where(Document.object_key == key)) and not db.scalar(
                        select(DocumentUpload.id).where(DocumentUpload.object_key == key,
                            DocumentUpload.state.in_(["uploading", "completing", "verifying"]))):
                    storage.delete(key)
