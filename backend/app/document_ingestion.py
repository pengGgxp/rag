"""One transactional document/version admission path for proxy and direct uploads."""
from uuid import uuid4
import hashlib
import logging
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import case, func, select

from .audit_log import record_audit
from .models import Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase, KnowledgeBaseStatus

MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".csv", ".xlsx"}


def latest_document_job(db, document):
    ready = document.status == DocumentStatus.READY
    return db.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id)
                     .order_by(IngestionJob.created_at.desc(), case(
                         (IngestionJob.state.in_((JobState.RUNNING, JobState.QUEUED)), 0),
                         (IngestionJob.state == JobState.SUCCEEDED, 1 if ready else 2),
                         else_=2 if ready else 1,
                     ), IngestionJob.updated_at.desc(), IngestionJob.id.desc()).limit(1))


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


def stage_upload(db, access, library_id, filename, content_type, stream, version_of=None, request=None, upload=None):
    from .storage import upload_fileobj, delete_object
    size, digest = inspect_file(stream)
    check_format(stream, filename)
    key = f"{library_id}/{uuid4()}{Path(filename).suffix.lower()}"
    document, job = admit(db, access, library_id, filename, content_type, size, digest, key, version_of, request)
    try:
        (upload or upload_fileobj)(fileobj=stream, object_key=key, content_type=content_type)
    except Exception:
        db.rollback()
        try:
            delete_object(key)
        except Exception:
            logging.getLogger(__name__).warning("Upload object cleanup deferred")
        raise
    return document, job


def commit_upload(db, document):
    from .storage import delete_object
    key = document.object_key
    try:
        db.commit()
    except Exception:
        db.rollback()
        try:
            delete_object(key)
        except Exception:
            logging.getLogger(__name__).warning("Upload object cleanup deferred")
        raise


def dispatch_ingestion(job_id, dispatch=None):
    from .tasks import process_document
    (dispatch or process_document.delay)(str(job_id))


def check_format(stream, filename):
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    header = stream.read(5)
    stream.seek(0)
    if suffix not in SUFFIXES or (suffix == ".pdf" and header != b"%PDF-") or (
            suffix in (".docx", ".xlsx") and not header.startswith(b"PK")):
        raise HTTPException(415, "文件内容与文档格式不符")


def admit(db, access, knowledge_base_id, filename, content_type, size, digest, key, version_of=None, request=None):
    from .storage_billing import check_capacity
    check_capacity(db, access.workspace_id, size, exclude_key=key)
    library = access.library(db, knowledge_base_id, write=True)
    # Serialize hash admission and version allocation for the same library.
    db.get(KnowledgeBase, library.id, with_for_update=True, populate_existing=True)
    previous = db.get(Document, version_of) if version_of else None
    if version_of and (not previous or previous.knowledge_base_id != library.id or previous.status == DocumentStatus.DELETED):
        raise HTTPException(404, "原文档版本不存在")
    if db.scalar(select(Document.id).where(Document.knowledge_base_id == library.id,
            Document.content_hash == digest, Document.status != DocumentStatus.DELETED)):
        raise HTTPException(409, "此文档已存在")
    number = (db.scalar(select(func.max(Document.version_number)).where(
        Document.version_group_id == previous.version_group_id)) + 1) if previous else 1
    document = Document(knowledge_base_id=library.id, filename=filename, content_type=content_type,
        size_bytes=size, content_hash=digest, object_key=key,
        version_group_id=previous.version_group_id if previous else uuid4(), version_number=number,
        supersedes_id=previous.id if previous else None, is_active=not bool(previous))
    db.add(document)
    db.flush()
    job = IngestionJob(document_id=document.id, submission_pending=True)
    db.add(job)
    library.status = KnowledgeBaseStatus.PROCESSING
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="document.version_uploaded" if previous else "document.uploaded", resource_type="document",
        resource_id=document.id, request=request,
        metadata={"knowledge_base_id": str(library.id), "filename": filename, "size_bytes": size, "version_number": number})
    db.flush()
    return document, job
