"""Current workspace files, directories and general file operations."""
import hashlib
from pathlib import PurePosixPath
from uuid import UUID, uuid4
from io import BytesIO
from typing import Literal
import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, Form
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import Access, get_access, now
from .db import get_db
from .models import ChatAttachment, WorkspaceDirectory, WorkspaceFile, WorkspaceFileVersion
from . import chat_attachments, workspace_files

router = APIRouter(prefix="/resources", tags=["resources"])
DIRECTORY_MARKER = ".rag-directory"


class PathPayload(BaseModel):
    path: str = Field(default="", max_length=512)
    new_path: str | None = Field(default=None, max_length=512)
    expected_version: int | None = Field(default=None, ge=1)
    revision: str | None = None
    confirmation: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class BatchPayload(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=10000)
    destination: str = Field(default="", max_length=512)
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    conflict: Literal["error", "keep_both", "replace", "merge"] = "error"
    expected: dict[str, str] = Field(default_factory=dict)
    resolutions: dict[str, Literal["error", "keep_both", "replace", "merge"]] = Field(default_factory=dict)
    confirmation: str | None = None


def _path(path: str, *, allow_root=True) -> str:
    path = path or ""
    if not path:
        if allow_root:
            return ""
        raise HTTPException(422, "路径不能为空")
    return workspace_files.valid_path(path)


def _children(db, access, current: str, query: str, sort: str, direction: str, source: str):
    global_search = bool(query.strip())
    prefix = f"{current}/" if current and not global_search else ""
    rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(prefix, autoescape=True))).all())
    directories = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.owner_user_id == access.user_id,
        WorkspaceDirectory.path.startswith(prefix, autoescape=True))).all())
    dirs: dict[str, dict] = {}
    for row in directories:
        relative = row.path[len(prefix):] if prefix and row.path.startswith(prefix) else row.path
        if not relative:
            continue
        if global_search:
            if query.casefold() in row.path.casefold():
                dirs[row.path] = {"kind": "directory", "name": relative.rsplit("/", 1)[-1], "path": row.path, "updated_at": row.updated_at.isoformat()}
        elif "/" not in relative:
            dirs[relative] = {"kind": "directory", "name": relative, "path": row.path, "updated_at": row.updated_at.isoformat()}
        else:
            child = relative.split("/", 1)[0]
            dirs.setdefault(child, {"kind": "directory", "name": child, "path": f"{prefix}{child}"})
    files: dict[str, dict] = {}
    for row in rows:
        relative = row.path[len(prefix):] if prefix and row.path.startswith(prefix) else row.path
        if not relative:
            continue
        if global_search:
            if source and row.source != source:
                continue
            if needle := query.casefold():
                if needle not in row.path.casefold():
                    continue
            item = workspace_files.read_file(row)
            item.update(kind="file", name=relative.rsplit("/", 1)[-1])
            files[row.path] = item
        elif "/" in relative:
            child = relative.split("/", 1)[0]
            dirs.setdefault(child, {"kind": "directory", "name": child, "path": f"{prefix}{child}".strip("/")})
        elif not source or row.source == source:
            item = workspace_files.read_file(row)
            item.update(kind="file", name=relative)
            files[relative] = item
    items = list(dirs.values()) + list(files.values())
    from .workspace_mutations import snapshot, tree_revision
    all_files, all_dirs, _ = snapshot(db, access, refresh=False)
    for item in items:
        if item["kind"] == "directory":
            item["revision"] = tree_revision([item["path"]], all_files, all_dirs)
    if query and not global_search:
        needle = query.casefold()
        items = [item for item in items if needle in item["path"].casefold()]
    if sort == "type":
        key = lambda item: ("0" if item["kind"] == "directory" else "1", item.get("content_type", "").casefold(), item["name"].casefold())
    elif sort == "updated_at":
        key = lambda item: item.get("updated_at", "")
    else:
        key = lambda item: item["name"].casefold()
    items.sort(key=key, reverse=direction == "desc")
    return items


def _breadcrumbs(path: str):
    parts = path.split("/") if path else []
    return [{"name": "我的资源", "path": ""}] + [
        {"name": part, "path": "/".join(parts[:index + 1])} for index, part in enumerate(parts)
    ]


def _workspace_row(db, access, path: str):
    return db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))


@router.get("/browse")
def browse(path: str = "", q: str = "", sort: str = Query("name", pattern="^(name|type|updated_at)$"),
        direction: str = Query("asc", pattern="^(asc|desc)$"), source: str = "", offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500), db=Depends(get_db), access: Access = Depends(get_access)):
    current = _path(path)
    from .workspace_mutations import snapshot
    from .file_operations import workspace_lock
    with workspace_lock(db, access.user_id):
        _, directories, _ = snapshot(db, access)
        if current and current not in directories:
            raise HTTPException(404, "文件夹不存在或已移动，请返回上级后重新选择")
        items = _children(db, access, current, q, sort, direction, source)
    return {"path": current, "parent_path": current.rsplit("/", 1)[0] if "/" in current else None,
        "breadcrumbs": _breadcrumbs(current), "items": items[offset:offset + limit],
        "offset": offset, "limit": limit, "has_more": len(items) > offset + limit, "total": len(items)}


@router.post("/directories", status_code=201)
def create_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import create_directory as create
    return create(db, access, _path(payload.path, allow_root=False), request_id=payload.request_id)


@router.put("/directories")
def rename_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import transfer
    old, new = _path(payload.path, allow_root=False), _path(payload.new_path or "", allow_root=False)
    result = transfer(db, access, [old], move=True, rename=new, expected={old: payload.revision} if payload.revision else {}, request_id=payload.request_id)
    return {**result, "path": new}


@router.delete("/directories", status_code=204)
def delete_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import delete
    delete(db, access, [_path(payload.path, allow_root=False)], confirmation=payload.confirmation, request_id=payload.request_id)


@router.put("/files/{identifier}")
def rename_file(identifier: UUID, payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import transfer
    from .file_operations import workspace_lock, fingerprint, replay
    target = _path(payload.new_path or "", allow_root=False)
    # File identity and request fields survive a successful rename; row.path does not.
    signature = fingerprint(["rename_file", identifier, payload.model_dump(exclude={"request_id"})])
    with workspace_lock(db, access.user_id):
        row = db.get(WorkspaceFile, identifier, populate_existing=True)
        if row is None or row.account_id != access.user_id:
            raise HTTPException(404, "文件不存在")
        previous = replay(db, access, payload.request_id, signature)
        if previous is not None:
            return previous["file"]
        if row.deleted_at:
            raise HTTPException(404, "文件不存在")
        result = transfer(db, access, [row.path], move=True, rename=target,
            expected={row.path: payload.revision} if payload.revision else {}, request_id=payload.request_id,
            signature=signature)
        return result["file"]


@router.post("/operations/move")
def move_items(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    return _transfer(payload, db, access, True)


@router.post("/operations/copy")
def copy_items(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    return _transfer(payload, db, access, False)


def _transfer(payload, db, access, move):
    from .workspace_mutations import transfer
    return transfer(db, access, payload.paths, _path(payload.destination), move=move,
        policy=payload.conflict, resolutions=payload.resolutions, expected=payload.expected, request_id=payload.request_id)


@router.post("/operations/delete-preview")
def preview_delete(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import delete_preview
    return delete_preview(db, access, payload.paths)


@router.post("/operations/delete")
def delete_items(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    from .workspace_mutations import delete
    if not payload.confirmation:
        raise HTTPException(428, "请先确认删除范围")
    return delete(db, access, payload.paths, confirmation=payload.confirmation, request_id=payload.request_id)


def verify_pending_objects(db, head):
    """Metadata migration never pretends that retained objects were verified."""
    versions = db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.available.is_(None),
        WorkspaceFileVersion.deleted_at.is_(None)).order_by(WorkspaceFileVersion.id).limit(500)).all()
    for version in versions:
        try:
            metadata = head(version.storage_key)
        except Exception:
            continue
        version.available = metadata is not None
        if metadata is None:
            for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.object_key == version.storage_key)):
                attachment.object_deleted_at = now()
        if metadata:
            size = metadata.get("Content-Length", metadata.get("ContentLength"))
            if size is not None:
                version.available = int(size) == version.size_bytes
            if version.available is False:
                for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.object_key == version.storage_key)):
                    attachment.object_deleted_at = now()
    db.commit()


@router.get("")
def listing(q: str = "", source: str = "", offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500), db=Depends(get_db), access: Access=Depends(get_access)):
    from .shared_resources import refresh_index
    discovered, has_more = refresh_index(db, access, q, offset, limit, source)
    query = select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.ilike(f"%{q}%"))
    if source:
        query = query.where(WorkspaceFile.source == source)
    rows = list(db.scalars(query.order_by(WorkspaceFile.created_at.desc(), WorkspaceFile.id)
        .offset(offset).limit(limit + 1)))
    if discovered is not None:
        return {"items": discovered, "offset": offset, "limit": limit, "has_more": has_more}
    return {"items": [workspace_files.read_file(row) for row in rows[:limit]],
        "offset": offset, "limit": limit, "has_more": len(rows) > limit}


@router.post("", status_code=201)
def upload(file: UploadFile, directory: str = Form(""), conflict: Literal["error", "keep_both", "replace"] = Form("error"),
        revision: str | None = Form(None), request_id: str | None = Form(None), db=Depends(get_db), access: Access=Depends(get_access)):
    from .workspace_mutations import save_files, snapshot, unique_name
    from .file_operations import workspace_lock, fingerprint, replay
    parent = _path(directory)
    name = workspace_files.valid_path(file.filename or "")
    if "/" in name:
        raise HTTPException(422, "请选择单个文件名，目录请通过当前位置指定")
    data = file.file.read(workspace_files.MAX_BYTES + 1)
    if len(data) > workspace_files.MAX_BYTES:
        raise HTTPException(413, "文件不能超过 200 MiB")
    signature = fingerprint(["upload", parent, name, conflict, revision, hashlib.sha256(data).hexdigest()])
    with workspace_lock(db, access.user_id):
        previous = replay(db, access, request_id, signature)
        if previous is not None:
            return previous["items"][0]
        files, dirs, _ = snapshot(db, access)
        if parent and parent not in dirs:
            raise HTTPException(404, "目标文件夹不存在，请刷新后选择")
        path = f"{parent}/{name}" if parent else name
        if conflict == "keep_both" and (path in files or path in dirs):
            path = unique_name(path, set(files) | dirs)
        expected = {path: revision} if conflict == "replace" and revision else {}
        result = save_files(db, access, {path: (data, mimetypes.guess_type(path)[0] or "application/octet-stream")},
            expected=expected, request_id=request_id, signature=signature)
        return result["items"][0]


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    from .shared_resources import refresh_current_metadata
    return workspace_files.read_file(refresh_current_metadata(db, access, workspace_files.owned(db, access, access.user_id, identifier)))


@router.api_route("/{identifier}/download", methods=["GET", "HEAD"])
def download(identifier: UUID, request: Request, version: int | None = Query(None, ge=1),
        preview: bool = False, db=Depends(get_db), access: Access=Depends(get_access)):
    from .storage_downloads import download_response
    row, selected = workspace_files.resolve_input(db, access, {"id": str(identifier)})
    # Active HTML/SVG/JavaScript are always attachments, never same-origin executable previews.
    inline = preview and (selected.content_type in {"image/png", "image/jpeg", "image/webp", "application/pdf"}
        or selected.content_type.startswith("text/plain"))
    return download_response(db, access, request, object_key=selected.storage_key,
        size=selected.size_bytes, filename=PurePosixPath(row.path).name, content_type=selected.content_type,
        resource=f"user_download:{row.id}", inline=inline)


@router.delete("/{identifier}", status_code=204)
def remove(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    row = workspace_files.owned(db, access, access.user_id, identifier, write=True)
    from .workspace_mutations import delete
    delete(db, access, [row.path])


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
    row = ChatAttachment(id=uuid4(), user_id=access.user_id, account_id=access.user_id,
        filename=PurePosixPath(resource.path).name, object_key=selected.storage_key,
        content_type=selected.content_type, content_hash=selected.content_hash, size_bytes=selected.size_bytes,
        kind="image" if image else "document", source=resource.source, resource_path=resource.path,
        resource_file_id=resource.id, resource_version=1, width=width, height=height,
        text_content=None if image else chat_attachments.extract_document(data, resource.path))
    db.add(row)
    db.commit()
    return chat_attachments.attachment_read(row)
