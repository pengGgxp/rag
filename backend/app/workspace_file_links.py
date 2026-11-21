"""Resolve one current workspace object without scanning or downloading it."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from .models import WorkspaceFile

ROOT = "/home/user/workspace/"


def workspace_path(value):
    from .workspace_files import valid_path
    if not isinstance(value, str):
        raise HTTPException(422, "文件路径无效")
    return valid_path(value[len(ROOT):] if value.startswith(ROOT) else value)


def file_reference(value):
    fields = ("id", "path", "content_type", "size_bytes", "version", "revision", "availability", "url")
    return {key: value[key] for key in fields if key in value}


def get_file_link(db, access, arguments):
    from . import shared_resources
    from .workspace_files import _current_file_version, owned, read_file
    from .file_operations import workspace_lock
    if (set(arguments) not in ({"id"}, {"path"})
            or not isinstance(next(iter(arguments.values()), None), str)):
        raise HTTPException(422, "请只提供一个文件 ID 或路径")
    access = access.revalidate(db)
    with workspace_lock(db, access.user_id):
        if "id" in arguments:
            try:
                identifier = UUID(arguments["id"])
            except (ValueError, TypeError):
                raise HTTPException(422, "资源 ID 无效") from None
            row = owned(db, access, access.user_id, identifier)
            path = row.path
        else:
            path = workspace_path(arguments["path"])
            row = db.scalar(select(WorkspaceFile).where(
                WorkspaceFile.owner_user_id == access.user_id, WorkspaceFile.path == path))
        key = (_current_file_version(db, row).storage_key if row and not row.deleted_at
               else shared_resources.object_key(access.user_id, path))
        try:
            storage = shared_resources.backend(db)
            if storage is None:
                from .storage_backend import current_storage
                storage = current_storage(db)
            metadata = storage.head(key)
        except Exception:
            raise HTTPException(503, "文件暂时无法核验，请稍后重试") from None
        if metadata is None:
            raise HTTPException(404, "文件不存在或已删除")
        if str(metadata.get("Type", "")).lower() == "symlink":
            raise HTTPException(422, "文件路径不能为符号链接")
        try:
            size = int(metadata.get("Content-Length", metadata.get("ContentLength", metadata.get("Size"))))
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(503, "文件暂时无法核验，请稍后重试") from None
        metadata = {**metadata, "Content-Length": size}
        if row is None or row.deleted_at:
            shared_resources.index_file(db, access, path, size=size)
            row = db.scalar(select(WorkspaceFile).where(
                WorkspaceFile.owner_user_id == access.user_id, WorkspaceFile.path == path,
                WorkspaceFile.deleted_at.is_(None)))
            row.live_etag = metadata.get("ETag")
        else:
            row = shared_resources.refresh_current_metadata(db, access, row, metadata=metadata)
        db.commit()
        return {**read_file(row), "url": f"/resources?file={row.id}"}
