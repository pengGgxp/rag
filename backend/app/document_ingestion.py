"""One transactional document/version admission path for proxy and direct uploads."""
from uuid import uuid4
import hashlib
import logging
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .audit_log import record_audit
from .models import (Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase,
                     KnowledgeBaseStatus)

MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".csv", ".xlsx"}


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


def stage_upload(db, access, library_id, filename, content_type, stream, version_of=None, request=None, upload=None,
                 request_id=None, content_origin="uploaded", workspace_path=None, expected_revision=None,
                 check_expected_revision=False, capacity_exclude_keys=(), already_uploaded_key=None):
    from .shared_resources import backend as shared_backend
    if shared_backend(db) is None:
        from .storage import delete_object, upload_fileobj
        size, digest = inspect_file(stream)
        check_format(stream, filename)
        key = already_uploaded_key or f"{library_id}/{uuid4()}{Path(filename).suffix.lower()}"
        document, job = admit(db, access, library_id, filename, content_type, size, digest, key,
            version_of, request, content_origin=content_origin,
            capacity_exclude_key=key if already_uploaded_key else None,
            capacity_exclude_keys=capacity_exclude_keys)
        if already_uploaded_key is None:
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

    from .workspace_mutations import snapshot, unique_name, save_files
    from .workspace_files import valid_path
    from .file_operations import file_revision
    from .knowledge_workspace import ensure_roots, migrate_legacy_documents
    size, digest = inspect_file(stream)
    check_format(stream, filename)
    from .models import Document
    library = access.library(db, library_id, write=True)
    ensure_roots(db, access.workspace_id, access)
    if version_of:
        previous = db.get(Document, version_of)
        if not previous or previous.knowledge_base_id != library.id or previous.status == DocumentStatus.DELETED:
            raise HTTPException(404, "原文档版本不存在")
        migrate_legacy_documents(db, access, library)
        previous = db.get(Document, version_of, populate_existing=True)
        path = workspace_path or previous.workspace_path or valid_path(f"{library.workspace_path}/{filename}")
    else:
        duplicate = db.scalar(select(Document.id).where(
            Document.knowledge_base_id == library.id,
            Document.content_hash == digest,
            Document.status != DocumentStatus.DELETED))
        path = workspace_path or valid_path(f"{library.workspace_path}/{filename}")

    files, directories, _ = snapshot(db, access)
    if request_id:
        from .knowledge_workspace import operation_id, synchronize
        from .models import KnowledgeBaseFileSnapshot
        receipt = db.scalar(select(KnowledgeBaseFileSnapshot).where(
            KnowledgeBaseFileSnapshot.workspace_id == access.workspace_id,
            KnowledgeBaseFileSnapshot.request_id == operation_id(access.workspace_id, request_id)))
        if receipt:
            replayed = db.scalar(select(Document).where(
                Document.knowledge_base_id == library.id,
                Document.workspace_path == path,
                Document.content_hash == digest,
                Document.status != DocumentStatus.DELETED,
            ).order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
            if replayed is None and receipt.state == "prepared" and path in files:
                from .knowledge_workspace import _file_bytes
                from .shared_resources import backend as shared_backend, object_key
                store = shared_backend(db)
                if store is not None:
                    live_size, live_digest = _file_bytes(store, object_key(access.workspace_id, path), filename)
                    if live_size == size and live_digest == digest:
                        synchronize(db, access, request_id, dispatch=lambda _job_id: None,
                            knowledge_base_id=library.id, content_origin=content_origin)
                        replayed = db.scalar(select(Document).where(
                            Document.knowledge_base_id == library.id,
                            Document.workspace_path == path,
                            Document.content_hash == digest,
                            Document.status != DocumentStatus.DELETED,
                        ).order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
            if replayed is not None:
                job = db.scalar(select(IngestionJob).where(IngestionJob.document_id == replayed.id)
                    .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc()).limit(1))
                if job is None or job.state in (JobState.FAILED, JobState.CANCELLED):
                    job = IngestionJob(document_id=replayed.id)
                    db.add(job)
                    replayed.status = DocumentStatus.QUEUED
                    replayed.error_detail = None
                    db.flush()
                return replayed, job
    if not version_of and duplicate:
        raise HTTPException(409, "此文档已存在")
    actual_revision = file_revision(files[path]) if path in files else None
    if check_expected_revision and expected_revision != actual_revision:
        raise HTTPException(409, "目标文件已变化，请刷新后重新上传")
    if path in files and not version_of:
        path = unique_name(path, set(files) | directories)
    expected = {path: file_revision(files[path]) if path in files else None}
    receipt = request_id or uuid4()
    from .shared_resources import object_key
    result = save_files(db, access, {path: (stream.read(MAX_DOCUMENT_BYTES + 1), content_type)},
        expected=expected, request_id=str(receipt), source="knowledge_base",
        content_origin=content_origin, dispatch_knowledge=False,
        capacity_exclude_key=object_key(access.workspace_id, path),
        capacity_exclude_keys=capacity_exclude_keys)
    if not result.get("items"):
        raise HTTPException(503, "文件已保存，但入库状态尚未确认，请查询知识库后重试")
    document = db.scalar(select(Document).where(
        Document.workspace_path == path, Document.knowledge_base_id == library.id,
        Document.content_hash == digest, Document.status != DocumentStatus.DELETED)
        .order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
    if document is None:
        from .knowledge_workspace import synchronize
        sync_result = synchronize(db, access, receipt, dispatch=lambda _job_id: None,
            knowledge_base_id=library.id, content_origin=content_origin,
            capacity_exclude_key=object_key(access.workspace_id, path),
            capacity_exclude_keys=capacity_exclude_keys)
        document = db.scalar(select(Document).where(
            Document.workspace_path == path, Document.knowledge_base_id == library.id,
            Document.content_hash == digest, Document.status != DocumentStatus.DELETED)
            .order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
        if document is None:
            raise HTTPException(503, "文件已保存，但入库任务尚未创建，请查询知识库后重试")
    job = db.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id)
        .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc()).limit(1))
    if job is None:
        job = IngestionJob(document_id=document.id)
        db.add(job)
        document.status = DocumentStatus.QUEUED
        db.flush()
    if content_origin != "uploaded":
        document.content_origin = content_origin
    return document, job


def commit_upload(db, document):
    try:
        db.commit()
    except Exception:
        db.rollback()
        if not getattr(document, "workspace_path", None):
            from .storage import delete_object
            try:
                delete_object(document.object_key)
            except Exception:
                logging.getLogger(__name__).warning("Upload object cleanup deferred")
        raise


def dispatch_ingestion(job_id, dispatch=None):
    from .tasks import process_document
    (dispatch or process_document.delay)(str(job_id))


def dispatch_document_job(job, *, parse_dispatch=None, embedding_dispatch=None):
    """Resume at parsing or embedding according to the durable document state."""
    document = job.document
    if document.parsed_text and document.chunks:
        from .tasks import embed_document
        (embedding_dispatch or embed_document.delay)(str(document.id), str(job.id))
    else:
        dispatch_ingestion(job.id, dispatch=parse_dispatch)


def check_format(stream, filename):
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    header = stream.read(5)
    stream.seek(0)
    if suffix not in SUFFIXES or (suffix == ".pdf" and header != b"%PDF-") or (
            suffix in (".docx", ".xlsx") and not header.startswith(b"PK")):
        raise HTTPException(415, "文件内容与文档格式不符")


def admit(db, access, knowledge_base_id, filename, content_type, size, digest, key, version_of=None, request=None,
          workspace_file_id=None, workspace_path=None, content_origin="uploaded",
          capacity_exclude_key=None, capacity_exclude_keys=(), workspace_shared=False):
    from .storage_billing import check_capacity
    check_capacity(db, access.workspace_id, size, exclude_key=capacity_exclude_key or key,
        exclude_keys=capacity_exclude_keys)
    if workspace_shared:
        access.editor()
        library = db.scalar(select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.workspace_id == access.workspace_id,
        ))
        if library is None:
            raise HTTPException(404, "知识库不存在或无权访问")
    else:
        library = access.library(db, knowledge_base_id, write=True)
    # Serialize hash admission and version allocation for the same library.
    db.get(KnowledgeBase, library.id, with_for_update=True, populate_existing=True)
    previous = db.get(Document, version_of) if version_of else None
    if version_of and (not previous or previous.knowledge_base_id != library.id or previous.status == DocumentStatus.DELETED):
        raise HTTPException(404, "原文档版本不存在")
    if workspace_file_id:
        existing = db.scalar(select(Document).where(
            Document.knowledge_base_id == library.id,
            Document.workspace_file_id == workspace_file_id,
            Document.content_hash == digest,
            Document.status != DocumentStatus.DELETED,
        ).order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
        if existing:
            existing.workspace_path = workspace_path
            existing.object_key = key
            existing.filename = filename
            existing.content_type, existing.size_bytes = content_type, size
            latest = db.scalar(select(IngestionJob).where(IngestionJob.document_id == existing.id)
                .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc()).limit(1))
            if existing.status in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING) and latest and latest.state in (
                    JobState.QUEUED, JobState.RUNNING):
                return existing, latest
            if existing.status == DocumentStatus.READY and latest and latest.state == JobState.SUCCEEDED:
                db.execute(update(Document).where(
                    Document.version_group_id == existing.version_group_id).values(is_active=False))
                existing.is_active = True
                library.status = KnowledgeBaseStatus.READY
                db.flush()
                return existing, latest
            job = IngestionJob(document_id=existing.id)
            db.add(job)
            existing.status = DocumentStatus.QUEUED
            existing.error_detail = None
            existing.is_active = False
            existing.content_origin = content_origin
            library.status = KnowledgeBaseStatus.PROCESSING
            db.flush()
            return existing, job
    if not workspace_file_id and db.scalar(select(Document.id).where(Document.knowledge_base_id == library.id,
            Document.content_hash == digest, Document.status != DocumentStatus.DELETED)):
        raise HTTPException(409, "此文档已存在")
    number = (db.scalar(select(func.max(Document.version_number)).where(
        Document.version_group_id == previous.version_group_id)) + 1) if previous else 1
    document = Document(knowledge_base_id=library.id, filename=filename, content_type=content_type,
        size_bytes=size, content_hash=digest, object_key=key,
        version_group_id=previous.version_group_id if previous else uuid4(), version_number=number,
        supersedes_id=previous.id if previous else None, is_active=not bool(previous))
    if workspace_file_id:
        document.workspace_file_id = workspace_file_id
        document.workspace_path = workspace_path
    document.content_origin = content_origin
    db.add(document)
    db.flush()
    job = IngestionJob(document_id=document.id)
    db.add(job)
    library.status = KnowledgeBaseStatus.PROCESSING
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="document.version_uploaded" if previous else "document.uploaded", resource_type="document",
        resource_id=document.id, request=request,
        metadata={"knowledge_base_id": str(library.id), "filename": filename, "size_bytes": size, "version_number": number})
    db.flush()
    return document, job
