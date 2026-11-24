"""Private browser previews with current-original checks for knowledge documents."""
import hashlib
import json
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .identity.auth import Access, get_access, now
from .db import get_db
from .models import ChatAttachment, Document, DocumentStatus
from .storage_backend import current_storage
from .workspace_files import _current_file_version, owned

router = APIRouter(tags=["file-preview-access"])
TTL = 300


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str | None = Field(default=None, max_length=64)


def kind(filename, content_type):
    suffix = PurePosixPath(filename).suffix.lower()
    if content_type in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif", "image/bmp"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pptx":
        return "pptx"
    if content_type.startswith("text/") or suffix in {
        ".md", ".json", ".xml", ".svg", ".js", ".jsx", ".ts", ".tsx", ".py", ".yaml", ".yml", ".toml", ".sh", ".css", ".html",
    }:
        return "text"
    return "unsupported"


def grant(storage, key, *, identifier, filename, content_type, expected, response, document=False):
    metadata = storage.head(key)
    if metadata is None:
        raise HTTPException(404, "文件已删除或不可用，请刷新后重试")
    etag = metadata.get("ETag") or metadata.get("etag")
    size = int(metadata.get("Content-Length", metadata.get("ContentLength", metadata.get("content-length", 0))))
    # A revision is about the object, not a renderer or an expiring capability URL.
    revision = hashlib.sha256(json.dumps([str(identifier), etag,
        str(metadata.get("Last-Modified", metadata.get("LastModified", ""))), size]).encode()).hexdigest()
    if expected and expected != revision:
        raise HTTPException(409, "文件已更新，请重新打开预览")
    headers = {"If-Match": etag} if etag else {}
    if not etag:
        raise HTTPException(503, "暂时无法确认文件状态，请稍后重试")
    response.headers["Cache-Control"] = "private, no-store"
    return {"id": str(identifier), "filename": PurePosixPath(filename).name, "content_type": content_type,
        "kind": kind(filename, content_type), "size_bytes": size, "revision": revision,
        "etag": etag, "headers": headers,
        "url": (f"/documents/{identifier}/preview-content?revision={revision}" if document
                else storage.read_url(key, seconds=TTL, inline=True)),
        "expires_in": TTL, "expires_at": now().timestamp() + TTL}


@router.post("/resources/{identifier}/preview-access")
def resource_access(identifier: UUID, payload: PreviewRequest, response: Response,
                    db=Depends(get_db), access: Access = Depends(get_access)):
    row = owned(db, access, access.user_id, identifier)
    return grant(current_storage(db), _current_file_version(db, row).storage_key, identifier=row.id,
        filename=row.path, content_type=row.content_type, expected=payload.revision, response=response)


@router.post("/chat/attachments/{identifier}/preview-access")
def attachment_access(identifier: UUID, payload: PreviewRequest, response: Response,
                      db=Depends(get_db), access: Access = Depends(get_access)):
    row = db.get(ChatAttachment, identifier)
    if not row or row.deleted_at or row.object_deleted_at or row.user_id != access.user_id:
        raise HTTPException(404, "附件不存在或无权访问")
    if row.resource_file_id:
        resource = owned(db, access, access.user_id, row.resource_file_id)
        key = _current_file_version(db, resource).storage_key
        filename, content_type = resource.path, resource.content_type
    else:
        key, filename, content_type = row.object_key, row.filename, row.content_type
    return grant(current_storage(db), key, identifier=row.id, filename=filename,
        content_type=content_type, expected=payload.revision, response=response)


def document_row(db, access, identifier):
    row = db.get(Document, identifier)
    if row is None or not row.is_active:
        raise HTTPException(404, "文件不存在或无权访问")
    access.library(db, row.knowledge_base_id)
    if row.status == DocumentStatus.DELETED:
        raise HTTPException(404, "文件已删除")
    return row


@router.post("/documents/{identifier}/preview-access")
def document_access(identifier: UUID, payload: PreviewRequest, response: Response,
                    db=Depends(get_db), access: Access = Depends(get_access)):
    row = document_row(db, access, identifier)
    return grant(current_storage(db), row.object_key, identifier=row.id, filename=row.filename,
        content_type=row.content_type, expected=payload.revision, response=response, document=True)


@router.get("/documents/{identifier}/preview-content")
def document_preview_content(identifier: UUID, revision: str, request: Request, response: Response,
                             db=Depends(get_db), access: Access = Depends(get_access)):
    from .file_operations import workspace_lock
    from .storage_downloads import byte_range

    with workspace_lock(db, access.user_id):
        row = document_row(db, access, identifier)
        store = current_storage(db)
        data = grant(store, row.object_key, identifier=row.id, filename=row.filename,
                     content_type=row.content_type, expected=revision, response=response, document=True)
        if request.headers.get("if-match") != data["etag"]:
            raise HTTPException(412, "文件已更新，请重新打开预览")
        size = data["size_bytes"]
        start, end, partial = byte_range(request.headers.get("range"), size)
        source = store.download_range(row.object_key, start, end) if partial else store.download(row.object_key)

    def body():
        with source:
            while block := source.read(1024 * 1024):
                yield block

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "ETag": data["etag"],
               "Cache-Control": "private, no-store",
               "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
               "Content-Disposition": "attachment"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=206 if partial else 200, headers=headers,
                             media_type="application/octet-stream")


@router.api_route("/documents/{identifier}/download", methods=["GET", "HEAD"])
def document_download(identifier: UUID, request: Request,
                      db=Depends(get_db), access: Access = Depends(get_access)):
    from .file_operations import workspace_lock
    from .storage_downloads import download_response

    with workspace_lock(db, access.user_id):
        row = document_row(db, access, identifier)
        return download_response(db, access, request, object_key=row.object_key, size=row.size_bytes,
            filename=row.filename, content_type=row.content_type, resource=f"document:{row.id}")
