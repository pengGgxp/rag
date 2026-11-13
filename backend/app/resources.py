"""Workspace-shared resources; uploads reuse the chat attachment validation service."""
from pathlib import PurePosixPath
from uuid import UUID
from io import BytesIO

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


class BatchPayload(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    destination: str = Field(default="", max_length=512)


def _path(path: str, *, allow_root=True) -> str:
    path = (path or "").strip().strip("/")
    if not path:
        if allow_root:
            return ""
        raise HTTPException(422, "路径不能为空")
    return workspace_files.valid_path(path)


def _children(db, access, current: str, query: str, sort: str, direction: str, source: str):
    global_search = bool(query.strip())
    prefix = f"{current}/" if current and not global_search else ""
    rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(prefix))).all())
    directories = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
        WorkspaceDirectory.path.startswith(prefix))).all())
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
    return [{"name": "工作区", "path": ""}] + [
        {"name": part, "path": "/".join(parts[:index + 1])} for index, part in enumerate(parts)
    ]


def _workspace_row(db, access, path: str):
    return db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))


@router.get("/browse")
def browse(path: str = "", q: str = "", sort: str = Query("name", pattern="^(name|type|updated_at)$"),
        direction: str = Query("asc", pattern="^(asc|desc)$"), source: str = "", offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500), db=Depends(get_db), access: Access = Depends(get_access)):
    current = _path(path)
    items = _children(db, access, current, q, sort, direction, source)
    return {"path": current, "parent_path": current.rsplit("/", 1)[0] if "/" in current else None,
        "breadcrumbs": _breadcrumbs(current), "items": items[offset:offset + limit],
        "offset": offset, "limit": limit, "has_more": len(items) > offset + limit}


@router.post("/directories", status_code=201)
def create_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); path = _path(payload.path, allow_root=False)
    if _workspace_row(db, access, path) or db.scalar(select(WorkspaceDirectory).where(
            WorkspaceDirectory.workspace_id == access.workspace_id, WorkspaceDirectory.path == path)):
        raise HTTPException(409, "目标位置已存在同名文件或文件夹")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if parent and not db.scalar(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
            WorkspaceDirectory.path == parent)):
        # Parent directories may be implicit when files were created by the cloud computer.
        if not db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
                WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(parent + "/"))):
            raise HTTPException(404, "上级文件夹不存在")
    row = WorkspaceDirectory(workspace_id=access.workspace_id, path=path, created_by=access.user_id)
    db.add(row)
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    marker_key = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, path)
    if storage:
        try:
            storage.put(marker_key, BytesIO(b""), "application/x-directory")
        except Exception:
            db.rollback()
            raise
    try:
        db.commit()
    except Exception:
        db.rollback()
        if storage:
            try: storage.delete(marker_key)
            except Exception: pass
        raise
    return {"kind": "directory", "name": path.rsplit("/", 1)[-1], "path": path}


def _rename_files(db, access, old: str, new: str, *, commit=True, source_keys=None):
    rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), (WorkspaceFile.path == old) | WorkspaceFile.path.startswith(old + "/"))))
    if not rows:
        raise HTTPException(404, "文件或文件夹不存在")
    if old == new:
        return {"path": new}
    if _path_exists(db, access, new):
        raise HTTPException(409, "目标位置已存在同名文件或文件夹")
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    copied = []
    try:
        for row in sorted(rows, key=lambda item: len(item.path)):
            target = new + row.path[len(old):]
            source_key = __import__("app.shared_resources", fromlist=["object_key"]).object_key(access.workspace_id, row.path)
            target_key = __import__("app.shared_resources", fromlist=["object_key"]).object_key(access.workspace_id, target)
            if storage:
                _copy_object(storage, source_key, target_key, row.live_etag, row.content_type)
                copied.append((source_key, target_key))
            row.path = target
            version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == 1))
            if version: version.object_key = target_key
            for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id,
                    ChatAttachment.deleted_at.is_(None))):
                attachment.resource_path = target
                attachment.object_key = target_key
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        for source_key, target_key in copied:
            try:
                storage.delete(target_key)
            except Exception:
                pass
        raise
    if storage:
        if source_keys is not None:
            source_keys.extend(source_key for source_key, _ in copied)
        else:
            for source_key, _ in copied:
                storage.delete(source_key)
    return {"path": new}


def _path_exists(db, access, path: str):
    return bool(_workspace_row(db, access, path) or db.scalar(select(WorkspaceDirectory).where(
        WorkspaceDirectory.workspace_id == access.workspace_id, WorkspaceDirectory.path == path)))


def _is_directory(db, access, path: str):
    return bool(db.scalar(select(WorkspaceDirectory).where(
        WorkspaceDirectory.workspace_id == access.workspace_id, WorkspaceDirectory.path == path)) or
        db.scalar(select(WorkspaceFile.id).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(path + "/"))))


def _copy_object(storage, source_key, target_key, etag, content_type):
    if hasattr(storage, "copy"):
        storage.copy(source_key, target_key, etag=etag)
    else:
        storage.put(target_key, storage.download(source_key), content_type)


@router.put("/directories")
def rename_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); old, new = _path(payload.path, allow_root=False), _path(payload.new_path or "", allow_root=False)
    if old == new:
        return {"path": new}
    if new.startswith(old + "/"):
        raise HTTPException(409, "不能将文件夹移动到自身内部")
    row = db.scalar(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id, WorkspaceDirectory.path == old))
    if not row and not _children(db, access, old, "", "name", "asc"):
        raise HTTPException(404, "文件夹不存在")
    source_keys = []
    marker_source = None
    result = _rename_files(db, access, old, new, commit=False, source_keys=source_keys) if _is_directory(db, access, old) and db.scalar(select(WorkspaceFile.id).where(
        WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(old + "/"))) else {"path": new}
    dirs = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
        (WorkspaceDirectory.path == old) | WorkspaceDirectory.path.startswith(old + "/"))))
    for item in dirs:
        item.path = new + item.path[len(old):]
    if row:
        row.path = new
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if storage:
        marker_old = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, old)
        marker_new = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, new)
        try:
            if storage.head(marker_old):
                _copy_object(storage, marker_old, marker_new, None, "application/x-directory")
                marker_source = marker_old
        except Exception:
            db.rollback()
            raise
    db.commit()
    if storage:
        for source_key in source_keys:
            storage.delete(source_key)
        if marker_source:
            storage.delete(marker_source)
    return result


@router.delete("/directories", status_code=204)
def delete_directory(payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); path = _path(payload.path, allow_root=False)
    directory = db.scalar(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
        WorkspaceDirectory.path == path))
    rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), (WorkspaceFile.path == path) | WorkspaceFile.path.startswith(path + "/"))))
    if not directory and not rows:
        raise HTTPException(404, "文件夹不存在")
    from .shared_resources import delete_path
    for row in rows:
        delete_path(db, access, row, commit=False)
    dirs = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
        (WorkspaceDirectory.path == path) | WorkspaceDirectory.path.startswith(path + "/"))))
    for item in dirs:
        db.delete(item)
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if storage:
        marker_key = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key
        for item in dirs:
            storage.delete(marker_key(access.workspace_id, item.path))
    db.commit()


@router.put("/files/{identifier}")
def rename_file(identifier: UUID, payload: PathPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); row = workspace_files.owned(db, access, access.workspace_id, identifier, write=True)
    new = _path(payload.new_path or "", allow_root=False)
    if db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.path == new, WorkspaceFile.id != row.id, WorkspaceFile.deleted_at.is_(None))):
        raise HTTPException(409, "目标位置已存在同名文件")
    old = row.path; _rename_files(db, access, old, new)
    return workspace_files.read_file(row)


@router.post("/operations/move")
def move_items(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); destination = _path(payload.destination)
    if destination and not _is_directory(db, access, destination):
        raise HTTPException(404, "目标文件夹不存在")
    source_keys = []
    marker_source_keys = []
    for path in payload.paths:
        old = _path(path, allow_root=False)
        name = old.rsplit("/", 1)[-1]
        target = f"{destination}/{name}" if destination else name
        if old == target: continue
        if target.startswith(old + "/"):
            raise HTTPException(409, "不能将文件夹移动到自身内部")
        if _workspace_row(db, access, target) or db.scalar(select(WorkspaceDirectory).where(
                WorkspaceDirectory.workspace_id == access.workspace_id, WorkspaceDirectory.path == target)):
            raise HTTPException(409, "目标位置已存在同名文件或文件夹")
        is_dir = _is_directory(db, access, old)
        has_file_tree = bool(_workspace_row(db, access, old) or db.scalar(select(WorkspaceFile.id).where(
            WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(old + "/"))))
        if has_file_tree: _rename_files(db, access, old, target, commit=False, source_keys=source_keys)
        elif not is_dir:
            raise HTTPException(404, "文件或文件夹不存在")
        if is_dir:
            dirs = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
                (WorkspaceDirectory.path == old) | WorkspaceDirectory.path.startswith(old + "/"))))
            for item in dirs: item.path = target + item.path[len(old):]
            storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
            if storage:
                marker_old = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, old)
                marker_new = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, target)
                if storage.head(marker_old):
                    _copy_object(storage, marker_old, marker_new, None, "application/x-directory")
                    marker_source_keys.append(marker_old)
    db.commit()
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if storage:
        for source_key in source_keys:
            storage.delete(source_key)
        for marker_key in marker_source_keys:
            storage.delete(marker_key)
    return {"moved": payload.paths, "destination": destination}


@router.post("/operations/copy")
def copy_items(payload: BatchPayload, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor(); destination = _path(payload.destination)
    if destination and not _is_directory(db, access, destination):
        raise HTTPException(404, "目标文件夹不存在")
    storage = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    created_objects = []
    try:
        for path in payload.paths:
            old = _path(path, allow_root=False); name = old.rsplit("/", 1)[-1]; target = f"{destination}/{name}" if destination else name
            if target.startswith(old + "/"):
                raise HTTPException(409, "不能将文件夹复制到自身内部")
            if _path_exists(db, access, target):
                raise HTTPException(409, "目标位置已存在同名文件或文件夹")
            rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
                WorkspaceFile.deleted_at.is_(None), (WorkspaceFile.path == old) | WorkspaceFile.path.startswith(old + "/"))))
            directory = db.scalar(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
                WorkspaceDirectory.path == old))
            if not rows and not directory:
                raise HTTPException(404, "文件或文件夹不存在")
            for row in rows:
                new_path = target + row.path[len(old):]
                source_key = __import__("app.shared_resources", fromlist=["object_key"]).object_key(access.workspace_id, row.path)
                target_key = __import__("app.shared_resources", fromlist=["object_key"]).object_key(access.workspace_id, new_path)
                if storage:
                    _copy_object(storage, source_key, target_key, row.live_etag, row.content_type); created_objects.append(target_key)
                clone = WorkspaceFile(workspace_id=access.workspace_id, path=new_path, content_type=row.content_type,
                    size_bytes=row.size_bytes, content_hash=row.content_hash, current_version=1, source=row.source,
                    created_by=access.user_id, updated_by=access.user_id, expires_at=row.expires_at)
                db.add(clone); db.flush()
                db.add(WorkspaceFileVersion(file_id=clone.id, workspace_id=access.workspace_id, version=1,
                    object_key=target_key, content_type=row.content_type, size_bytes=row.size_bytes,
                    content_hash=row.content_hash, created_by=access.user_id))
            dirs = list(db.scalars(select(WorkspaceDirectory).where(WorkspaceDirectory.workspace_id == access.workspace_id,
                (WorkspaceDirectory.path == old) | WorkspaceDirectory.path.startswith(old + "/"))))
            for item in dirs:
                db.add(WorkspaceDirectory(workspace_id=access.workspace_id, path=target + item.path[len(old):], created_by=access.user_id))
                if storage:
                    source_marker = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, item.path)
                    target_marker = __import__("app.shared_resources", fromlist=["directory_marker_key"]).directory_marker_key(access.workspace_id, target + item.path[len(old):])
                    if storage.head(source_marker):
                        _copy_object(storage, source_marker, target_marker, None, "application/x-directory"); created_objects.append(target_marker)
        db.commit()
    except Exception:
        db.rollback()
        if storage:
            for key in created_objects:
                try: storage.delete(key)
                except Exception: pass
        raise
    return {"copied": payload.paths, "destination": destination}


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
    discovered, has_more = refresh_index(db, access, q, offset, limit, source)
    query = select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
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
def upload(file: UploadFile, directory: str = Form(""), db=Depends(get_db), access: Access=Depends(get_access)):
    result = chat_attachments.create_upload(file, db, access, source="resource_upload", directory=_path(directory))
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
