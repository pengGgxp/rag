"""Scoped, resumable multipart uploads. Only the server can seal or admit an object."""
import hashlib
import math
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select, func, or_, text
from sqlalchemy.exc import OperationalError

from ...identity.auth import Access, aware, now, get_access
from ...db import get_db, SessionLocal
from .failures import classify, database_sqlstate, failure, log_worker_failure, rejection
from .admission import publish, replacement_target, check_format, MAX_DOCUMENT_BYTES, SUFFIXES
from .jobs import latest_document_job
from ..schemas import PartInput, UploadInput
from ...models import Document, DocumentStatus, DocumentUpload, User
from ...storage_backend import current_storage
from ...upstream_retries import retry_after

router = APIRouter(tags=["document-uploads"])
PART_SIZE = 8 * 1024 * 1024


def view(row, parts=None):
    return {"id": str(row.id), "knowledge_base_id": str(row.knowledge_base_id), "filename": row.filename,
        "size_bytes": row.size_bytes, "part_size": PART_SIZE, "state": row.state,
        "error": row.failure.get("message") if row.failure else ("上传任务未完成，请重试" if row.error else None),
        "failure": row.failure,
        "verify_after": row.verify_after.isoformat() if row.verify_after else None,
        "expires_at": row.expires_at.isoformat(), "parts": parts or [],
        "document_id": str(row.document_id) if row.document_id else None}


def owned(db, access, identifier, *, lock=True):
    row = db.get(DocumentUpload, identifier, with_for_update=lock, populate_existing=True)
    if not row or row.account_id != access.user_id or row.user_id != access.user_id:
        raise HTTPException(404, "上传任务不存在")
    access.library(db, row.knowledge_base_id, write=True)
    if aware(row.expires_at) <= now() and row.state not in ("completed", "cancelled", "cleaned"):
        raise rejection(410, "upload", "upload_expired", "上传已过期，请重新选择文件")
    return row


def cloud_parts(storage, row):
    parts = storage.parts(row.object_key, row.multipart_id)
    if isinstance(parts, dict):
        parts = [parts]
    return sorted(parts, key=lambda part: int(part["PartNumber"]))


@router.post("/knowledge-bases/{library_id}/uploads", status_code=201)
def create_upload(library_id: UUID, payload: UploadInput, idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
                  db=Depends(get_db), access=Depends(get_access)):
    access.library(db, library_id, write=True)
    request_key = idempotency_key or payload.request_key
    # Lock before opening a storage multipart session so concurrent replays share one.
    lock_key = int.from_bytes(hashlib.sha256(
        f"{access.user_id}:{library_id}:{request_key}".encode()
    ).digest()[:8], "big", signed=True)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    prior = db.scalar(select(DocumentUpload).where(
        DocumentUpload.account_id == access.user_id, DocumentUpload.user_id == access.user_id,
        DocumentUpload.knowledge_base_id == library_id, DocumentUpload.request_key == request_key,
    ).with_for_update())
    if prior:
        if (prior.filename, prior.content_type, prior.size_bytes, prior.version_of, prior.expected_content_hash) != (
                payload.filename, payload.content_type, payload.size_bytes,
                payload.replace_document_id, payload.expected_content_hash):
            raise rejection(409, "upload", "idempotency_conflict",
                            "此上传请求已用于其他文件，请重新选择文件")
        result = view(prior)
        if aware(prior.expires_at) <= now() and prior.state not in ("completed", "cancelled", "cleaned"):
            result["state"] = "expired"
            result["failure"] = failure(
                "upload", "upload_expired", "上传已过期，请重新选择文件后上传",
                request_id=str(prior.id),
            )
            result["error"] = result["failure"]["message"]
        if result["state"] == "creating":
            return open_multipart(db, prior)
        return result
    if bool(payload.replace_document_id) != bool(payload.expected_content_hash):
        raise HTTPException(422, "替换原件需要当前文件摘要")
    previous = replacement_target(db, library_id, payload.replace_document_id) if payload.replace_document_id else None
    if previous and previous.content_hash != payload.expected_content_hash:
        raise HTTPException(412, "原件已经更新，请刷新后重新选择文件")
    from ...storage_billing import check_capacity
    check_capacity(db, access.user_id, payload.size_bytes, replaced_bytes=previous.size_bytes if previous else 0)
    if (Path(payload.filename).suffix.lower() not in SUFFIXES or "/" in payload.filename
            or "\\" in payload.filename or any(ord(c) < 32 for c in payload.filename + payload.content_type)):
        raise rejection(415, "upload", "invalid_format", "不支持此文件格式，或文件名无效")
    if db.scalar(select(func.count()).select_from(DocumentUpload).where(
            DocumentUpload.user_id == access.user_id, DocumentUpload.state.in_(["creating", "uploading", "completing", "verifying"]),
            DocumentUpload.expires_at > now())) >= 20:
        raise rejection(429, "upload", "upload_limit", "进行中的上传过多，请先完成或取消部分任务")
    identifier = uuid4()
    key = f"_knowledge_uploads/{library_id}/{identifier}{Path(payload.filename).suffix.lower()}"
    row = DocumentUpload(id=identifier, user_id=access.user_id, account_id=access.user_id,
        knowledge_base_id=library_id, filename=payload.filename, content_type=payload.content_type,
        size_bytes=payload.size_bytes, version_of=payload.replace_document_id, object_key=key,
        expected_content_hash=payload.expected_content_hash,
        multipart_id="", request_key=request_key, state="creating",
        expires_at=now()+timedelta(hours=24))
    db.add(row)
    db.commit()
    row = owned(db, access, identifier)
    return open_multipart(db, row)


def open_multipart(db, row):
    if row.state != "creating":
        return view(row)
    storage = current_storage(db)
    # The reservation owns this object key even if the provider response was lost.
    existing = next((item["UploadId"] for item in storage.incomplete_uploads(prefix=row.object_key)
                     if item["Key"] == row.object_key), None)
    if existing is None:
        existing = storage.begin(row.object_key, row.content_type)
    row.multipart_id, row.state = existing, "uploading"
    db.commit()
    return view(row)


@router.get("/uploads/{identifier}")
def read_upload(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier, lock=False)
    parts = cloud_parts(current_storage(db), row) if row.state == "uploading" else []
    result = view(row, [{"number": int(part["PartNumber"]), "size": int(part["Size"]),
        "sha256": row.parts.get(str(part["PartNumber"]))} for part in parts])
    if row.document_id:
        document = db.get(Document, row.document_id, populate_existing=True)
        if document:
            result["document_status"] = document.status.value
            job = latest_document_job(db, document)
            if job:
                from .failures import safe_job_failure
                result["job_id"] = str(job.id)
                job_failure = safe_job_failure(job, document)
                if job_failure:
                    result["failure"] = job_failure
                result["retry_progress"] = job.retry_progress
                result["retry_after"] = job.retry_after.isoformat() if job.retry_after else None
                result["job_state"] = job.state.value
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
        raise rejection(409, "upload", "integrity_mismatch", "文件内容与原上传不一致，请重新选择文件上传")
    size = min(PART_SIZE, row.size_bytes - (number-1)*PART_SIZE)
    row.parts = {**row.parts, str(number): payload.sha256}
    url = current_storage(db).part_url(row.object_key, row.multipart_id, number, size)
    db.commit()
    return {"url": url, "expires_in": 900, "size": size}


@router.post("/uploads/{identifier}/complete", status_code=202)
def complete_upload(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    if row.state == "failed" and row.failure and row.failure.get("retryable"):
        row.state, row.error, row.failure = "verifying", None, None
        row.verify_attempts, row.verify_wait_ms, row.verify_after = 0, 0, None
        db.commit()
        from ...tasks import verify_upload
        verify_upload.delay(str(row.id))
        return view(row)
    if row.state in ("completed", "verifying"):
        if row.state == "verifying" and row.verify_after is None:
            from ...tasks import verify_upload
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
        row.state = "failed"
        row.failure = failure("verification", "integrity_mismatch", "文件实际大小不符，请重新上传",
                              request_id=str(row.id))
        row.error = row.failure["message"]
        db.commit()
        try:
            storage.delete(row.object_key)
        except Exception:
            pass
        return view(row)
    row.state = "verifying"
    db.commit()
    from ...tasks import verify_upload
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
    from ...file_operations import workspace_lock
    with SessionLocal() as db:
        row = db.get(DocumentUpload, UUID(identifier))
        if not row:
            return
        account_id = row.account_id
        db.rollback()
        with workspace_lock(db, account_id):
            _finalize_upload(identifier)


def _finalize_upload(identifier):
    from ...tasks import process_document
    with SessionLocal() as db:
        row = db.get(DocumentUpload, UUID(identifier), with_for_update=True, populate_existing=True)
        if (not row or row.state != "verifying" or aware(row.expires_at) <= now()
                or (row.verify_after is not None and aware(row.verify_after) > now())):
            return
        storage = None
        try:
            storage = current_storage(db)
            access = worker_access(db, row)
            access.library(db, row.knowledge_base_id, write=True)
            with storage.download(row.object_key) as stream:
                from .admission import inspect_file
                size, digest = inspect_file(stream)
                if size != row.size_bytes:
                    raise HTTPException(409, "文件实际大小不一致")
                check_format(stream, row.filename)
                content = stream.read()
            # Verify the actual content against all browser part fingerprints.
            for number in range(1, math.ceil(size / PART_SIZE)+1):
                chunk = content[(number-1)*PART_SIZE:number*PART_SIZE]
                if hashlib.sha256(chunk).hexdigest() != row.parts.get(str(number)):
                    raise HTTPException(409, "文件摘要校验失败，请重新上传")
            def finish(document, job, queued):
                db.refresh(row, with_for_update=True)
                if row.state != "verifying" or aware(row.expires_at) <= now():
                    raise HTTPException(409, "上传已经取消或过期")
                worker_access(db, row)
                row.document_id, row.state = document.id, "completed"
                row.error, row.failure, row.verify_after = None, None, None

            key = row.object_key
            document, job, queued = publish(db, access, row.knowledge_base_id, row.filename, row.content_type,
                size, digest, key, replace_id=row.version_of, expected_hash=row.expected_content_hash,
                finish=finish)
        except Exception as error:
            db.rollback()
            row = db.get(DocumentUpload, UUID(identifier), with_for_update=True, populate_existing=True)
            if row and row.state == "verifying":
                row.verify_attempts += 1
                row.failure = classify_verification_error(error, row)
                log_worker_failure(
                    stage="verification",
                    route="/uploads/{upload_id}/complete",
                    request_id=row.failure["request_id"],
                    upload_id=str(row.id),
                    document_id=str(row.document_id) if row.document_id else None,
                    status=error.status_code if isinstance(error, HTTPException) else None,
                    code=row.failure["code"],
                    retryable=bool(row.failure["retryable"]),
                    exception_type=type(error).__name__,
                    sqlstate=database_sqlstate(error),
                )
                if row.failure["retryable"] and row.verify_attempts < 4:
                    headers = getattr(error, "headers", None) or {}
                    after = retry_after(next((v for k, v in headers.items() if k.lower() == "retry-after"), None))
                    delay_ms = math.ceil(1000 * max(after or 0, 2 ** (row.verify_attempts - 1) + random.uniform(0, 0.5)))
                else:
                    delay_ms = None
                if delay_ms is not None and row.verify_wait_ms + delay_ms <= 60_000:
                    row.verify_wait_ms += delay_ms
                    delay = delay_ms / 1000
                    row.verify_after = now() + timedelta(seconds=delay)
                    db.commit()
                    from ...tasks import verify_upload
                    try:
                        verify_upload.apply_async(args=[str(row.id)], countdown=delay)
                    except Exception:
                        # The durable due timestamp lets maintenance redispatch it.
                        pass
                    return
                row.state = "failed"
                row.error = row.failure["message"]
                row.verify_after = None
                db.commit()
                if storage and not row.failure["retryable"]:
                    try:
                        storage.delete(row.object_key)
                    except Exception:
                        pass
            return
        try:
            storage.delete(key)
        except Exception:
            # Completed staging objects are cleaned by the orphan sweep.
            pass
        if queued:
            try:
                process_document.delay(str(job.id))
            except Exception:
                # Recovery redispatches the job from its durable submission flag.
                pass


def worker_access(db, row):
    user = db.get(User, row.account_id, populate_existing=True)
    if not user or user.is_suspended or row.user_id != row.account_id:
        raise HTTPException(403, "上传权限已失效")
    return Access(row.user_id, "")


def classify_verification_error(error, row):
    if isinstance(error, OperationalError):
        return classify("verification", error, request_id=str(row.id))
    if isinstance(error, HTTPException):
        status = error.status_code
        if status == 409 and any(word in str(error.detail) for word in ("大小", "摘要", "fingerprint", "size")):
            return failure("verification", "integrity_mismatch", "文件大小或摘要校验不符，请重新上传",
                           request_id=str(row.id))
        if status == 412:
            return failure("verification", "original_changed", str(error.detail), request_id=str(row.id))
        if status == 409 and any(word in str(error.detail) for word in ("已存在", "重复", "duplicate")):
            return failure("verification", "duplicate_document", "文档内容已存在，请查看文档列表",
                           request_id=str(row.id))
        if status == 409:
            return failure("verification", "verification_conflict", "校验状态已变化，请重试查询",
                           retryable=True, request_id=str(row.id))
        if status == 415:
            return failure("verification", "invalid_format", "文件内容与所选格式不符，请检查文件",
                           request_id=str(row.id))
        if status in (401, 403, 404):
            return failure("verification", "permission_denied", "上传权限已变化，请刷新页面后重试",
                           request_id=str(row.id))
        if status in (402, 413, 422) or isinstance(error.detail, dict):
            return classify("verification", error, request_id=str(row.id))
        if status == 429 or status >= 500:
            return failure("verification", "storage_unavailable", "文件暂时无法读取，请稍后重试",
                           retryable=True, request_id=str(row.id))
        return failure("verification", "validation_failed", "文件校验失败，请检查文件",
                       request_id=str(row.id))
    return failure("verification", "storage_unavailable", "服务暂时不可用，请稍后重试",
                   retryable=True, request_id=str(row.id))


def cleanup_uploads():
    with SessionLocal() as db:
        rows = db.scalars(select(DocumentUpload).where(DocumentUpload.expires_at <= now(),
            DocumentUpload.state.in_(["creating", "uploading", "completing", "verifying", "failed", "cancelled", "expired"]))
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
    from ...tasks import verify_upload, process_document
    from ...models import IngestionJob, JobState
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            from .recovery import recover_interrupted_ingestion
            recover_interrupted_ingestion(db)
        due = select(DocumentUpload).where(DocumentUpload.state == "verifying", DocumentUpload.expires_at > now(),
            DocumentUpload.verify_after.is_(None) | (DocumentUpload.verify_after <= now())
        ).with_for_update(skip_locked=True)
        for row in db.scalars(due):
            identifier = row.id
            verify_upload.delay(str(identifier))
        # Recover queued work whose broker delivery was lost or whose backoff elapsed.
        jobs = db.scalars(select(IngestionJob).where(
            IngestionJob.state == JobState.QUEUED,
            IngestionJob.submission_pending.is_(True),
            or_(IngestionJob.retry_after.is_(None), IngestionJob.retry_after <= now()),
        ).with_for_update(skip_locked=True)).all()
        for job in jobs:
            if job.phase == "indexing":
                from ...tasks import embed_document
                embed_document.delay(str(job.document_id), str(job.id))
            else:
                process_document.delay(str(job.id))


def cleanup_orphans():
    """Only platform-shaped objects older than the session lifetime are eligible."""
    from ...storage_backend import active_settings
    with SessionLocal() as db:
        settings = active_settings(db)
        if not settings or not settings.active:
            return
        storage = current_storage(db)
        cutoff = now() - timedelta(hours=24)
        managed = re.compile(r"^(?:_knowledge_uploads/)?[0-9a-f-]{36}/[0-9a-f-]{36}\.[a-z0-9]+$")
        def old(value):
            try:
                return aware(datetime.fromisoformat(value.replace("Z", "+00:00"))) < cutoff
            except (ValueError, TypeError, AttributeError):
                return False
        for candidate in storage.incomplete_uploads():
            key = candidate["Key"]
            if managed.fullmatch(key) and old(candidate.get("Initiated")) and not db.scalar(
                    select(DocumentUpload.id).where(DocumentUpload.object_key == key,
                        DocumentUpload.state.in_(["creating", "uploading", "completing", "verifying"]))):
                storage.abort(key, candidate["UploadId"])
        for candidate in storage.iter_objects():
            key = candidate["Key"]
            if not old(candidate.get("LastModified")):
                continue
            if key.startswith(("_ocr/", "_checks/")):
                storage.delete(key)
            elif re.fullmatch(r"_chat/[0-9a-f-]{36}/[0-9a-f-]{36}\.[a-z0-9]+", key):
                from ...models import ChatAttachment
                if not db.scalar(select(ChatAttachment.id).where(ChatAttachment.object_key == key)):
                    storage.delete(key)
            elif key.startswith("_knowledge_uploads/") and managed.fullmatch(key):
                # The legacy document prefix contains rollback originals after cutover.
                if not db.scalar(select(Document.id).where(Document.object_key == key)) and not db.scalar(
                        select(DocumentUpload.id).where(DocumentUpload.object_key == key,
                            DocumentUpload.expires_at > now(),
                            DocumentUpload.state.in_(["creating", "uploading", "completing", "verifying", "failed"]))):
                    storage.delete(key)
