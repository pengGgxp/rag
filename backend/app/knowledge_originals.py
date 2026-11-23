"""Storage layout for personal knowledge originals and their recycle bin."""
from pathlib import PurePosixPath

from . import file_operations as operations

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

    from .models import Chunk, Document, DocumentStatus

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
