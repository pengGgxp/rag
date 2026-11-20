"""Metadata helpers for the single, workspace-mounted resource directory."""
import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .auth import now
from .models import ChatAttachment, WorkspaceDirectory, WorkspaceFile, WorkspaceFileVersion

EXCLUDED = {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"}
DIRECTORY_MARKER = ".rag-directory"


def prefix(workspace_id):
    return f"workspaces/{workspace_id}/resource-directory/"


def backend(db):
    from .storage_backend import active_settings, current_storage
    settings = active_settings(db)
    return current_storage(db) if settings and settings.provider == "oss" else None


def object_key(workspace_id, path):
    return prefix(workspace_id) + path


def directory_marker_key(workspace_id, path):
    return object_key(workspace_id, f"{path.rstrip('/')}/{DIRECTORY_MARKER}")


def mount_config(db, workspace_id):
    storage = backend(db)
    if storage is None:
        return None, False
    return storage, prefix(workspace_id)


def index_file(db, access, path, *, size, content_type=None, digest="", source="model_output", identifier=None):
    """Upsert current metadata only; file bytes stay at the mounted object key."""
    from .storage_billing import lock_workspace
    from .workspace_files import MAX_BYTES, valid_path, read_file
    path = valid_path(path)
    if size < 0 or size > MAX_BYTES:
        raise HTTPException(413, "文件不能超过 200 MiB")
    lock_workspace(db, access.workspace_id)
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.path == path).execution_options(populate_existing=True))
    if row and row.deleted_at:
        for old in db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id)):
            db.delete(old)
        row.path = f"deleted/{row.id}/{uuid4().hex}"
        db.flush()
        row = None
    active = row is not None
    if row is None:
        row = WorkspaceFile(id=identifier or uuid4(), workspace_id=access.workspace_id, path=path,
            content_type=content_type or mimetypes.guess_type(path)[0] or "application/octet-stream",
            size_bytes=size, content_hash=digest or "", current_version=1, created_by=access.user_id,
            updated_by=access.user_id, source=source, expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
        db.add(row)
    else:
        row.content_type = content_type or mimetypes.guess_type(path)[0] or row.content_type
        row.size_bytes, row.content_hash = size, digest or row.content_hash or ""
        row.current_version = 1
        row.updated_by, row.source = access.user_id, source
    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id,
            ChatAttachment.deleted_at.is_(None))):
        if attachment.content_hash != digest or attachment.size_bytes != size:
            attachment.text_content = None
            attachment.width = attachment.height = None
        attachment.object_key = object_key(access.workspace_id, path)
        attachment.resource_version = 1
        attachment.size_bytes, attachment.content_type = size, row.content_type
        attachment.content_hash = digest or row.content_hash
        attachment.filename = path.rsplit("/", 1)[-1]
        attachment.object_deleted_at = None
    row.live_etag = None
    db.flush()
    key = object_key(access.workspace_id, path)
    current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
        WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
    if current is None:
        db.add(WorkspaceFileVersion(file_id=row.id, workspace_id=access.workspace_id, version=1,
            object_key=key, content_type=row.content_type, size_bytes=size, content_hash=digest or "",
            created_by=access.user_id))
    else:
        current.object_key, current.content_type = key, row.content_type
        current.size_bytes, current.content_hash = size, digest or ""
        current.deleted_at, current.available = None, True
    from .audit_log import record_audit
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="resource.updated" if active else "resource.created", resource_type="resource",
        resource_id=row.id, metadata={"source": source})
    db.flush()
    return read_file(row)


def refresh_index(db, access, q="", offset=0, limit=100, source="", *, commit=True,
                  path_prefix="", filename=None):
    from .file_operations import workspace_lock
    with workspace_lock(db, access.workspace_id):
        return _refresh_index(db, access, q, offset, limit, source, commit=commit,
            path_prefix=path_prefix, filename=filename)


def _refresh_index(db, access, q, offset, limit, source, *, commit, path_prefix="", filename=None):
    """Discover files created by shell commands from object metadata, never read bodies."""
    storage = backend(db)
    if storage is None:
        return None, False
    from .workspace_files import valid_path, read_file
    from .audit_log import record_audit
    from .storage_billing import lock_workspace
    root = prefix(access.workspace_id)
    objects, marked_directories = {}, set()
    for item in storage.iter_objects(root + path_prefix):
        key = item["Key"]
        if not key.startswith(root):
            raise HTTPException(502, "资源目录返回了无效文件")
        path = key[len(root):]
        if not path.startswith(path_prefix) or (filename and path.rsplit("/", 1)[-1].casefold() != filename.casefold()):
            continue
        if str(item.get("Type", "")).lower() == "symlink":
            continue
        if not path or any(part in EXCLUDED for part in path.split("/")):
            continue
        if path.rsplit("/", 1)[-1] == DIRECTORY_MARKER or path.endswith("/"):
            directory = path.rsplit("/", 1)[0]
            if directory:
                valid_path(directory)
                marked_directories.add(directory)
            continue
        valid_path(path)
        objects[path] = item
    rows = []
    lock_workspace(db, access.workspace_id)
    directories = {} if filename else {row.path: row for row in db.scalars(select(WorkspaceDirectory).where(
        WorkspaceDirectory.workspace_id == access.workspace_id,
        WorkspaceDirectory.path.startswith(path_prefix, autoescape=True)))}
    for path in marked_directories - directories.keys():
        db.add(WorkspaceDirectory(workspace_id=access.workspace_id, path=path, created_by=access.user_id))
    for path, row in directories.items():
        if path not in marked_directories:
            db.delete(row)
    for path, item in sorted(objects.items()):
        row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.path == path).execution_options(populate_existing=True))
        size = int(item.get("Size", item.get("size", 0)))
        if row is None:
            row = WorkspaceFile(id=uuid4(), workspace_id=access.workspace_id, path=path,
                content_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                size_bytes=size, content_hash="", current_version=1, created_by=access.user_id,
                updated_by=access.user_id, source="model_output", expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
            db.add(row)
            db.flush()
            db.add(WorkspaceFileVersion(file_id=row.id, workspace_id=access.workspace_id, version=1,
                object_key=object_key(access.workspace_id, path), content_type=row.content_type,
                size_bytes=size, content_hash="", created_by=access.user_id))
            record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
                action="resource.created", resource_type="resource", resource_id=row.id,
                metadata={"source": "cloud_computer"})
        elif row.deleted_at:
            for old in db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id)):
                db.delete(old)
            row.path = f"deleted/{row.id}/{uuid4().hex}"
            row.updated_by = access.user_id
            db.flush()
            row = WorkspaceFile(id=uuid4(), workspace_id=access.workspace_id, path=path,
                content_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                size_bytes=size, content_hash="", current_version=1, created_by=access.user_id,
                updated_by=access.user_id, source="model_output", expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
            db.add(row)
            db.flush()
            db.add(WorkspaceFileVersion(file_id=row.id, workspace_id=access.workspace_id, version=1,
                object_key=object_key(access.workspace_id, path), content_type=row.content_type,
                size_bytes=size, content_hash="", created_by=access.user_id))
        elif row.size_bytes != size:
            row.size_bytes = size
            row.content_hash = ""
            row.updated_by = access.user_id
            current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
                WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
            if current:
                current.object_key, current.size_bytes = object_key(access.workspace_id, path), size
                current.content_type, current.content_hash = row.content_type, ""
        etag = item.get("ETag")
        if etag and row.live_etag != etag:
            row.live_etag, row.content_hash = etag, ""
            row.updated_at, row.updated_by = now(), access.user_id
            current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
                WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
            if current:
                current.object_key, current.content_hash = object_key(access.workspace_id, path), ""
                current.size_bytes, current.content_type = size, row.content_type
        if q.lower() in path.lower() and (not source or row.source == source):
            rows.append(row)
    present = set(objects)
    missing_query = select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(path_prefix, autoescape=True))
    if filename:
        missing_query = missing_query.where(func.lower(WorkspaceFile.path).endswith("/" + filename.lower(), autoescape=True))
    for row in db.scalars(missing_query):
        if row.path in present:
            continue
        row.deleted_at = now()
        for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id)):
            attachment.deleted_at = row.deleted_at
            attachment.object_deleted_at = row.deleted_at
    if commit:
        db.commit()
    else:
        db.flush()
    end = None if limit is None else offset + limit
    return [read_file(row) for row in rows[offset:end]], end is not None and len(rows) > end


def remove_live(db, resource):
    storage = backend(db)
    if storage is not None:
        storage.delete(object_key(resource.workspace_id, resource.path))
    resource.live_etag = None
    return True


def delete_path(db, access, resource, *, remove_object=True, commit=True):
    from .storage_billing import lock_workspace
    from .auth import now as current_time
    access.editor()
    if resource.workspace_id != access.workspace_id:
        raise HTTPException(404, "资源不存在")
    lock_workspace(db, access.workspace_id)
    storage = backend(db)
    if storage is None:
        raise HTTPException(503, "资源目录尚未就绪")
    if remove_object:
        remove_live(db, resource)
    resource.deleted_at = current_time()
    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id)):
        attachment.deleted_at = resource.deleted_at
        attachment.object_deleted_at = resource.deleted_at
    from .audit_log import record_audit
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="resource.deleted", resource_type="resource", resource_id=resource.id,
        metadata={"source": "workspace_mount"})
    if commit:
        db.commit()


def register_operation(db, access, operation, value):
    """Update the metadata index after a direct mounted file operation; never copy bytes."""
    from .workspace_files import read_file
    name, path = operation["name"], operation.get("path", "")
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.path == path).execution_options(populate_existing=True))
    if name == "delete_file":
        if row and not row.deleted_at:
            delete_path(db, access, row, remove_object=False)
        return {**value, "deleted": True}
    if name not in {"write_file", "apply_patch"}:
        return value
    if not value.get("sha256") or len(value["sha256"]) != 64:
        raise HTTPException(502, "文件校验信息缺失，请重试")
    result = index_file(db, access, path, size=int(value["size_bytes"]),
        digest=value["sha256"], source="model_output")
    db.commit()
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.path == path))
    return read_file(row) if row else result


def refresh_current_metadata(db, access, resource, *, metadata=None):
    """Revalidate current mounted object metadata before a server-side image read."""
    from .storage_billing import lock_workspace
    from .workspace_files import owned
    access = access.revalidate(db) if access.session_hash else access
    resource = owned(db, access, access.workspace_id, resource.id)
    storage = backend(db)
    if storage is None and metadata is None:
        return resource
    lock_workspace(db, access.workspace_id)
    key = object_key(access.workspace_id, resource.path)
    metadata = storage.head(key) if metadata is None else metadata
    if metadata is None:
        resource.deleted_at = now()
        for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id)):
            attachment.deleted_at = resource.deleted_at
            attachment.object_deleted_at = resource.deleted_at
        db.commit()
        raise HTTPException(404, "资源已删除或不可用")
    size = int(metadata.get("Content-Length", metadata.get("ContentLength", resource.size_bytes)))
    etag = metadata.get("ETag")
    changed = size != resource.size_bytes or bool(etag and etag != resource.live_etag)
    if changed:
        resource.size_bytes, resource.content_hash, resource.live_etag = size, "", etag
        current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == resource.id,
            WorkspaceFileVersion.version == 1))
        if current:
            current.object_key, current.size_bytes, current.content_hash = key, size, ""
        for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id,
                ChatAttachment.deleted_at.is_(None))):
            attachment.size_bytes, attachment.content_hash, attachment.object_key = size, "", key
            attachment.text_content = None
            attachment.width = attachment.height = None
        db.commit()
    return resource
