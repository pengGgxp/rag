"""Publish verified originals and their ingestion jobs together."""
import hashlib
import logging
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import case, select

from .audit_log import record_audit
from .models import Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase, KnowledgeBaseStatus

MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".csv", ".xlsx"}


def job_matches_original(document, job):
    return (job.original_revision == document.original_revision
            and (job.original_hash is None or job.original_hash == document.content_hash))


def latest_document_job(db, document, *, prefer_ready=False):
    ready = prefer_ready or document.status == DocumentStatus.READY
    ordering = [case(
        (IngestionJob.state.in_((JobState.RUNNING, JobState.QUEUED)), 0),
        (IngestionJob.state == JobState.SUCCEEDED, 1 if ready else 2),
        else_=2 if ready else 1,
    )]
    if ready:
        ordering.append(case((IngestionJob.phase == "indexing", 0), else_=1))
    ordering.extend((IngestionJob.created_at.desc(), IngestionJob.updated_at.desc(), IngestionJob.id.desc()))
    return db.scalar(select(IngestionJob).where(
        IngestionJob.document_id == document.id,
        IngestionJob.original_revision == document.original_revision,
        (IngestionJob.original_hash == document.content_hash) | IngestionJob.original_hash.is_(None),
    ).order_by(*ordering).limit(1))


def inspect_file(stream):
    size, digest = 0, hashlib.sha256()
    stream.seek(0)
    while block := stream.read(1024 * 1024):
        size += len(block)
        if size > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "File exceeds 200 MB limit")
        digest.update(block)
    stream.seek(0)
    if not size:
        raise HTTPException(400, "Uploaded file is empty")
    return size, digest.hexdigest()


def check_format(stream, filename):
    suffix = Path(filename).suffix.lower()
    header = stream.read(5)
    stream.seek(0)
    if suffix not in SUFFIXES or (suffix == ".pdf" and header != b"%PDF-") or (
            suffix in (".docx", ".xlsx") and not header.startswith(b"PK")):
        raise HTTPException(415, "文件内容与文档格式不符")


def stage_upload(db, access, library_id, filename, content_type, stream, *, replace_id=None,
                 expected_hash=None, request=None, request_id=None, finish=None):
    from . import file_operations as ops

    if not filename or len(filename) > 255 or "/" in filename or "\\" in filename or any(
            ord(character) < 32 for character in filename):
        raise HTTPException(422, "文件名无效")
    size, digest = inspect_file(stream)
    check_format(stream, filename)
    key = f"_knowledge_uploads/{library_id}/{uuid4()}{Path(filename).suffix.lower()}"
    store = ops.storage(db)
    try:
        store.put(key, stream, content_type)
        return publish(db, access, library_id, filename, content_type, size, digest, key,
                       replace_id=replace_id, expected_hash=expected_hash, request=request,
                       request_id=request_id, finish=finish)
    finally:
        try:
            store.delete(key)
        except Exception:
            logging.getLogger(__name__).warning("Upload object cleanup deferred")


def dispatch_ingestion(job_id, dispatch=None):
    from .tasks import process_document
    (dispatch or process_document.delay)(str(job_id))


def replacement_target(db, library_id, identifier):
    previous = db.get(Document, identifier, populate_existing=True)
    if (not previous or previous.knowledge_base_id != library_id
            or previous.status == DocumentStatus.DELETED or not previous.is_active):
        raise HTTPException(404, "待替换文档不存在")
    return previous


def publish(db, access, library_id, filename, content_type, size, digest, key, *, replace_id=None,
            expected_hash=None, request=None, request_id=None, finish=None):
    from . import file_operations as ops
    from .knowledge_originals import copy_change, delete_change, original_key
    from .main import stop_ingestion_job
    from .storage_billing import check_capacity

    signature = ops.fingerprint([library_id, filename, content_type, digest, replace_id, expected_hash])
    with ops.workspace_lock(db, access.user_id):
        access.library(db, library_id, write=True)
        saved = ops.replay(db, access, request_id, signature)
        if saved is not None:
            document = db.get(Document, UUID(saved["document_id"]))
            return document, db.get(IngestionJob, UUID(saved["job_id"])) if saved.get("job_id") else None, False
        previous = replacement_target(db, library_id, replace_id) if replace_id else None
        if previous is not None and previous.content_hash == digest:
            job = latest_document_job(db, previous)
            if finish:
                finish(previous, job, False)
            if request_id:
                from .models import WorkspaceFileOperation
                db.add(WorkspaceFileOperation(account_id=access.user_id, user_id=access.user_id,
                    request_id=request_id, fingerprint=signature, state="completed", steps=[],
                    result={"document_id": str(previous.id), "job_id": str(job.id) if job else None}))
            db.commit()
            return previous, job, False
        if previous is not None and previous.content_hash != expected_hash:
            raise HTTPException(412, "原件已经更新，请刷新后重新选择文件")
        check_capacity(db, access.user_id, size, exclude_key=key,
                       replaced_bytes=previous.size_bytes if previous else 0)
        identifier = previous.id if previous else uuid4()
        old_key = previous.object_key if previous else None
        target = original_key(access.user_id, library_id, identifier, filename)
        store = ops.storage(db)
        changes = [copy_change(store, key, target, content_type, digest=digest, overwrite=old_key == target)]
        if old_key and old_key != target:
            changes.append(delete_change(store, old_key))

        def update_metadata():
            access.library(db, library_id, write=True)
            if previous:
                document = db.get(Document, identifier, with_for_update=True, populate_existing=True)
                if document.status == DocumentStatus.DELETED or document.content_hash != expected_hash:
                    raise HTTPException(412, "原件已经更新，请刷新后重新选择文件")
                for job in document.jobs:
                    if job.state in (JobState.QUEUED, JobState.RUNNING):
                        stop_ingestion_job(db, document, job, "原件已替换")
                document.chunks.clear()
                document.parsed_text = document.parsing_metadata = document.parser_name = None
            else:
                document = Document(id=identifier, knowledge_base_id=library_id, version_group_id=identifier)
                db.add(document)
            document.filename, document.content_type = filename, content_type
            document.size_bytes, document.content_hash, document.object_key = size, digest, target
            document.status, document.error_detail = DocumentStatus.QUEUED, None
            document.original_revision = uuid4()
            job = IngestionJob(document_id=identifier, original_hash=digest,
                               original_revision=document.original_revision, submission_pending=True)
            db.flush()
            db.add(job)
            library = db.get(KnowledgeBase, library_id, with_for_update=True, populate_existing=True)
            access.library(db, library_id, write=True)
            library.status = KnowledgeBaseStatus.PROCESSING
            record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
                action="document.replaced" if previous else "document.uploaded", resource_type="document",
                resource_id=identifier, request=request, metadata={
                    "knowledge_base_id": str(library_id), "filename": filename, "size_bytes": size})
            db.flush()
            if finish:
                finish(document, job, True)
            return {"document_id": str(identifier), "job_id": str(job.id)}

        receipt = ops.execute(db, access, changes, update_metadata, request_id=request_id, signature=signature)
        return db.get(Document, identifier), db.get(IngestionJob, UUID(receipt["job_id"])), True
