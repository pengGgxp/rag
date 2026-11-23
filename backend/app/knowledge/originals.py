"""Storage layout for personal knowledge originals and their recycle bin."""
import logging
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy import select

from .. import file_operations as operations
from ..audit_log import record_audit
from ..models import Document, DocumentStatus, EmbeddingStatus, IngestionJob, JobState, KnowledgeBaseStatus
from .ingestion import failures as document_failures

MOUNT_ROOT = "/home/user/knowledge"


def prefix(account_id):
    return f"accounts/{account_id}/knowledge-originals/"


def original_key(account_id, library_id, document_id, filename, *, deleted=False):
    root = f"accounts/{account_id}/knowledge-trash/" if deleted else prefix(account_id)
    return f"{root}{library_id}/{document_id}{PurePosixPath(filename).suffix.lower()}"


def mount_path(document, account_id):
    root = prefix(account_id)
    if document.is_active and document.status.value != "deleted" and document.object_key.startswith(root):
        return f"{MOUNT_ROOT}/{document.object_key[len(root):]}"
    return None


def ensure_mount_root(store, account_id):
    key = prefix(account_id) + ".rag-directory"
    if store.head(key) is None:
        import io
        store.put(key, io.BytesIO(), "application/octet-stream")


def copy_change(store, source, target, content_type, *, digest=None, overwrite=False):
    from fastapi import HTTPException

    before = store.head(source)
    if before is None:
        raise HTTPException(409, "知识库原件不可用")
    return {"key": target, "source": source, "source_revision": operations.revision(before),
            "expected": operations.revision(store.head(target)) if overwrite else None,
            "content_type": content_type, "digest": digest}


def delete_change(store, key):
    return {"key": key, "delete": True, "expected": operations.revision(store.head(key))}


def resolve_citation(db, access, citation, chunk_id):
    from uuid import UUID

    from fastapi import HTTPException
    from sqlalchemy import select

    from ..models import Chunk, Document, DocumentStatus

    try:
        identifier = UUID(str(citation.get("version_id") or citation.get("document_id")))
    except ValueError:
        identifier = None
    chunk = db.get(Chunk, chunk_id)
    if identifier is None and chunk is not None:
        identifier = chunk.document_id
    source = db.get(Document, identifier) if identifier else None
    if source is None:
        return {"state": "unknown"}
    try:
        library = access.library(db, source.knowledge_base_id)
    except HTTPException:
        return {"state": "unavailable"}
    current = source if source.is_active else db.scalar(select(Document).where(
        Document.knowledge_base_id == library.id, Document.version_group_id == source.version_group_id,
        Document.is_active.is_(True)).limit(1))
    if current is None or current.status == DocumentStatus.DELETED:
        return {"state": "unavailable"}
    digest = citation.get("original_hash")
    if not digest and not source.is_active:
        # Archived rows retain the digest of the historical original.
        digest = source.content_hash
    if not digest and chunk is not None and chunk.document_id == source.id and chunk.text == citation.get("text"):
        # Chunk identities are never reused on replacement.
        digest = source.content_hash
    if not digest:
        return {"state": "unknown"}
    if digest != current.content_hash:
        return {"state": "changed"}
    return {"state": "current", "document_id": str(current.id), "original_path": mount_path(current, access.user_id),
            "page_number": citation.get("page_number"),
            "url": f"/?library={library.id}&view=documents&document={current.id}" + (f"&page={citation['page_number']}"
                    if isinstance(citation.get("page_number"), int) and citation["page_number"] > 0 else "")}


def change_original_availability(db, access, document_id, request, *, deleted):
    from .ingestion.jobs import dispatch_retried_job, latest_document_job, stop_ingestion_job
    from .schemas import document_read

    with operations.workspace_lock(db, access.user_id):
        document = db.get(Document, document_id)
        if document is None or not document.is_active:
            raise HTTPException(404, "Document not found")
        access.library(db, document.knowledge_base_id, write=True)
        if (document.status == DocumentStatus.DELETED) == deleted:
            return document_read(document, latest_document_job(db, document))
        source = document.object_key
        target = original_key(access.user_id, document.knowledge_base_id, document.id,
                              document.filename, deleted=deleted)
        store = operations.storage(db)
        changes = [copy_change(store, source, target, document.content_type, digest=document.content_hash),
                   delete_change(store, source)]
        queued_job = None

        def update_metadata():
            nonlocal document, queued_job
            document = db.get(Document, document_id, with_for_update=True, populate_existing=True)
            library = access.library(db, document.knowledge_base_id, write=True)
            previous = latest_document_job(db, document, prefer_ready=not deleted and all(
                chunk.embedding_status == EmbeddingStatus.READY and chunk.embedding is not None
                for chunk in document.chunks) and bool(document.chunks))
            document.object_key = target
            if deleted:
                for job in document.jobs:
                    if job.state in (JobState.QUEUED, JobState.RUNNING):
                        stop_ingestion_job(db, document, job, "原件已删除")
                document.status = DocumentStatus.DELETED
                document.error_detail = None
                has_ready = db.scalar(select(Document.id).where(
                    Document.knowledge_base_id == library.id, Document.id != document.id,
                    Document.is_active.is_(True), Document.status == DocumentStatus.READY).limit(1))
                library.status = KnowledgeBaseStatus.READY if has_ready else KnowledgeBaseStatus.DRAFT
            else:
                failure = document_failures.safe_job_failure(previous, document) if previous else None
                ready = bool(document.chunks) and all(
                    chunk.embedding_status == EmbeddingStatus.READY and chunk.embedding is not None
                    for chunk in document.chunks)
                if failure and failure["code"] == "needs_reconciliation":
                    document.status, document.error_detail = DocumentStatus.FAILED, failure["message"]
                    library.status = KnowledgeBaseStatus.ERROR
                else:
                    document.error_detail = None
                    ready = ready and previous is not None and previous.phase == "indexing" and previous.state == JobState.SUCCEEDED
                    document.status = DocumentStatus.READY if ready else DocumentStatus.QUEUED
                    library.status = KnowledgeBaseStatus.READY if ready else KnowledgeBaseStatus.PROCESSING
                    if not ready:
                        queued_job = IngestionJob(document_id=document.id, original_hash=document.content_hash,
                            original_revision=document.original_revision,
                            phase="indexing" if document.parsed_text and document.chunks else "parsing",
                            metering_id=(previous.metering_id or previous.id) if previous else None,
                            submission_pending=True)
                        db.add(queued_job)
            record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
                action="document.deleted" if deleted else "document.restored", resource_type="document",
                resource_id=document.id, request=request,
                metadata={"knowledge_base_id": str(library.id), "filename": document.filename})

        operations.execute(db, access, changes, update_metadata)
        if queued_job:
            try:
                dispatch_retried_job(db, queued_job)
            except Exception:
                logging.getLogger(__name__).warning("Restored document dispatch deferred: %s", queued_job.id)
        return document_read(document, latest_document_job(db, document))
