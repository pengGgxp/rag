"""Workspace-shared resources; uploads reuse the chat attachment validation service."""
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from sqlalchemy import select

from .auth import Access, get_access, now
from .db import get_db
from .models import ChatAttachment, WorkspaceFile, WorkspaceFileVersion
from . import chat_attachments, workspace_files

router = APIRouter(prefix="/resources", tags=["resources"])


def verify_pending_objects(db, head):
    """Metadata migration never pretends that retained objects were verified."""
    versions = db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.available.is_(None),
        WorkspaceFileVersion.deleted_at.is_(None)).order_by(WorkspaceFileVersion.id).limit(500)).all()
    for version in versions:
        try:
            metadata = head(version.object_key)
        except Exception:
            continue
        version.available = metadata is not None
        if metadata is None:
            for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.object_key == version.object_key)):
                attachment.object_deleted_at = now()
        if metadata:
            size = metadata.get("Content-Length", metadata.get("ContentLength"))
            if size is not None:
                version.available = int(size) == version.size_bytes
            if version.available is False:
                for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.object_key == version.object_key)):
                    attachment.object_deleted_at = now()
    db.commit()


@router.get("")
def listing(q: str = "", source: str = "", offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500), db=Depends(get_db), access: Access=Depends(get_access)):
    from .shared_resources import refresh_index
    from .skill_directory import visible_rows
    discovered, has_more = refresh_index(db, access, q, offset, limit, source)
    query = select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.ilike(f"%{q}%"), *visible_rows())
    if source:
        query = query.where(WorkspaceFile.source == source)
    rows = list(db.scalars(query.order_by(WorkspaceFile.created_at.desc(), WorkspaceFile.id)
        .offset(offset).limit(limit + 1)))
    if discovered is not None:
        return {"items": discovered, "offset": offset, "limit": limit, "has_more": has_more}
    return {"items": [workspace_files.read_file(row) for row in rows[:limit]],
        "offset": offset, "limit": limit, "has_more": len(rows) > limit}


@router.post("", status_code=201)
def upload(file: UploadFile, db=Depends(get_db), access: Access=Depends(get_access)):
    result = chat_attachments.create_upload(file, db, access, source="resource_upload")
    resource = workspace_files.owned(db, access, access.workspace_id, UUID(result["resource_id"]))
    return workspace_files.read_file(resource)


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    return workspace_files.read_file(workspace_files.owned(db, access, access.workspace_id, identifier))


@router.api_route("/{identifier}/download", methods=["GET", "HEAD"])
def download(identifier: UUID, request: Request, version: int | None = Query(None, ge=1),
        preview: bool = False, db=Depends(get_db), access: Access=Depends(get_access)):
    from .storage_downloads import download_response
    row, selected = workspace_files.resolve_input(db, access, {"id": str(identifier)})
    # Active HTML/SVG/JavaScript are always attachments, never same-origin executable previews.
    inline = preview and (selected.content_type in {"image/png", "image/jpeg", "image/webp", "application/pdf"}
        or selected.content_type.startswith("text/plain"))
    from .shared_resources import object_key
    return download_response(db, access, request, object_key=object_key(access.workspace_id, row.path),
        size=row.size_bytes, filename=PurePosixPath(row.path).name, content_type=row.content_type,
        resource=f"user_download:{row.id}", inline=inline)


@router.delete("/{identifier}", status_code=204)
def remove(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    row = workspace_files.owned(db, access, access.workspace_id, identifier, write=True)
    from .shared_resources import delete_path
    delete_path(db, access, row)


@router.post("/{identifier}/attachment")
def attach(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    resource, selected = workspace_files.resolve_input(db, access, {"id": str(identifier)})
    row = db.scalar(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id,
        ChatAttachment.deleted_at.is_(None)))
    if row:
        return chat_attachments.attachment_read(row)
    suffix = PurePosixPath(resource.path).suffix.lower()
    if suffix not in chat_attachments.DOCUMENT_SUFFIXES | chat_attachments.IMAGE_TYPES.keys():
        raise HTTPException(415, "该格式暂不能作为对话附件，请在对话中通过资源工具读取")
    if selected.size_bytes > chat_attachments.MAX_BYTES:
        raise HTTPException(413, "对话附件不能超过 20 MiB")
    from .resource_cache import read_bytes
    data = read_bytes(db, access, resource, selected)
    image = suffix in chat_attachments.IMAGE_TYPES
    if image and len(data) > chat_attachments.MAX_IMAGE_BYTES:
        raise HTTPException(413, "对话图片不能超过 10 MiB")
    width, height = chat_attachments.image_size(data, suffix) if image else (None, None)
    if width and width * height > 40_000_000:
        raise HTTPException(413, "图片尺寸过大，请缩小后重试")
    row = ChatAttachment(id=uuid4(), user_id=access.user_id, workspace_id=access.workspace_id,
        filename=PurePosixPath(resource.path).name, object_key=selected.object_key,
        content_type=selected.content_type, content_hash=selected.content_hash, size_bytes=selected.size_bytes,
        kind="image" if image else "document", source=resource.source, resource_path=resource.path,
        resource_file_id=resource.id, resource_version=1, width=width, height=height,
        text_content=None if image else chat_attachments.extract_document(data, resource.path))
    db.add(row)
    db.commit()
    return chat_attachments.attachment_read(row)
