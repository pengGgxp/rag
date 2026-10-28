"""One transactional document/version admission path for proxy and direct uploads."""
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .audit_log import record_audit
from .models import Document, DocumentStatus, IngestionJob, KnowledgeBase, KnowledgeBaseStatus

MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".csv", ".xlsx"}


def check_format(stream, filename):
    from pathlib import Path
    suffix = Path(filename).suffix.lower()
    header = stream.read(5)
    stream.seek(0)
    if suffix not in SUFFIXES or (suffix == ".pdf" and header != b"%PDF-") or (
            suffix in (".docx", ".xlsx") and not header.startswith(b"PK")):
        raise HTTPException(415, "文件内容与文档格式不符")


def admit(db, access, knowledge_base_id, filename, content_type, size, digest, key, version_of=None, request=None):
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
    job = IngestionJob(document_id=document.id)
    db.add(job)
    library.status = KnowledgeBaseStatus.PROCESSING
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="document.version_uploaded" if previous else "document.uploaded", resource_type="document",
        resource_id=document.id, request=request,
        metadata={"knowledge_base_id": str(library.id), "filename": filename, "size_bytes": size, "version_number": number})
    db.flush()
    return document, job
