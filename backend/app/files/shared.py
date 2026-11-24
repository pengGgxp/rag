"""Metadata helpers for the single, workspace-mounted resource directory."""
import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.identity.auth import now
from app.models import ChatAttachment, CloudComputerSession, WorkspaceDirectory, WorkspaceFile, WorkspaceFileVersion

EXCLUDED = {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"}
DIRECTORY_MARKER = ".rag-directory"


def prefix(account_id):
    return f"accounts/{account_id}/resource-directory/"


def backend(db):
    from app.infra.storage_backend import active_settings, current_storage
    settings = active_settings(db)
    return current_storage(db) if settings and settings.provider == "oss" else None


def object_key(account_id, path):
    return prefix(account_id) + path


def directory_marker_key(account_id, path):
    return object_key(account_id, f"{path.rstrip('/')}/{DIRECTORY_MARKER}")


def stored_marker_key(directory):
    return (directory.mounted_marker_key or directory.marker_key
            or directory_marker_key(directory.account_id, directory.path))


def mount_config(db, account_id):
    storage = backend(db)
    if storage is None:
        return None, False
    return storage, prefix(account_id)


def materialize_legacy_mount(db, account_id):
    from io import BytesIO
    from app.files.operations import copy_object
    from app.migrate_shared_resources import _fingerprint
    from app.models import LegacyAccountObjectPrefix

    has_legacy = db.scalar(select(LegacyAccountObjectPrefix.prefix).where(
        LegacyAccountObjectPrefix.user_id == account_id).limit(1))
    if not has_legacy:
        return
    snapshots = db.scalars(select(CloudComputerSession.snapshot).where(
        CloudComputerSession.account_id == account_id,
        CloudComputerSession.ended_at.is_(None),
    ))
    if any(snapshot.get("resource_mount", {}).get("prefix", "").startswith("workspaces/")
           for snapshot in snapshots):
        raise HTTPException(409, "请先结束仍在运行的旧云电脑，再创建新实例")
    store = backend(db)
    if store is None:
        raise HTTPException(503, "资源目录暂不可用，请稍后重试")

    def copy(source, target):
        head = store.head(source)
        if head is None:
            raise HTTPException(409, "历史资源缺失，请联系管理员")
        if store.head(target) is None:
            copy_object(store, source, target, etag=head.get("ETag"), create=True)
        elif _fingerprint(store, source) != _fingerprint(store, target):
            raise HTTPException(409, "资源目录中已有不同内容的同名文件，请联系管理员")

    legacy_root = "workspaces/"
    files = db.execute(select(WorkspaceFile, WorkspaceFileVersion).join(
        WorkspaceFileVersion, (WorkspaceFileVersion.file_id == WorkspaceFile.id)
        & (WorkspaceFileVersion.version == WorkspaceFile.current_version)).where(
            WorkspaceFile.owner_user_id == account_id,
            WorkspaceFile.deleted_at.is_(None),
            WorkspaceFileVersion.deleted_at.is_(None),
            WorkspaceFileVersion.available.is_not(False),
            WorkspaceFileVersion.mounted_object_key.is_(None),
            WorkspaceFileVersion.object_key.startswith(legacy_root, autoescape=True)))
    for row, version in files:
        target = object_key(account_id, row.path)
        copy(version.object_key, target)
        version.mounted_object_key = target
        for attachment in db.scalars(select(ChatAttachment).where(
            ChatAttachment.resource_file_id == row.id, ChatAttachment.deleted_at.is_(None)
        )):
            attachment.object_key = target
    for directory in db.scalars(select(WorkspaceDirectory).where(
        WorkspaceDirectory.owner_user_id == account_id,
        WorkspaceDirectory.mounted_marker_key.is_(None),
        WorkspaceDirectory.marker_key.startswith(legacy_root, autoescape=True),
    )):
        target = directory_marker_key(account_id, directory.path)
        if store.head(directory.marker_key):
            copy(directory.marker_key, target)
        elif store.head(target) is None:
            if getattr(store, "conditional_create", False):
                store.put(target, BytesIO(), "application/x-directory", create=True)
            else:
                store.put(target, BytesIO(), "application/x-directory")
        directory.mounted_marker_key = target
    db.flush()


def index_file(db, access, path, *, size, content_type=None, digest="", source="model_output", identifier=None,
               key=None):
    """Upsert current metadata only; file bytes stay at the mounted object key."""
    from app.files.quotas import lock_workspace
    from app.files.workspace import MAX_BYTES, valid_path, read_file
    path = valid_path(path)
    if size < 0 or size > MAX_BYTES:
        raise HTTPException(413, "文件不能超过 200 MiB")
    lock_workspace(db, access.user_id)
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.path == path).execution_options(populate_existing=True))
    if row and row.deleted_at:
        for old in db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id)):
            db.delete(old)
        row.path = f"deleted/{row.id}/{uuid4().hex}"
        db.flush()
        row = None
    active = row is not None
    if row is None:
        row = WorkspaceFile(id=identifier or uuid4(), account_id=access.user_id, path=path,
            content_type=content_type or mimetypes.guess_type(path)[0] or "application/octet-stream",
            size_bytes=size, content_hash=digest or "", current_version=1, created_by=access.user_id,
            updated_by=access.user_id, source=source, expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
        db.add(row)
    else:
        row.content_type = content_type or mimetypes.guess_type(path)[0] or row.content_type
        row.size_bytes, row.content_hash = size, digest or row.content_hash or ""
        row.current_version = 1
        row.updated_by, row.source = access.user_id, source
    key = key or object_key(access.user_id, path)
    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id,
            ChatAttachment.deleted_at.is_(None))):
        if attachment.content_hash != digest or attachment.size_bytes != size:
            attachment.text_content = None
            attachment.width = attachment.height = None
        attachment.object_key = key
        attachment.resource_version = 1
        attachment.size_bytes, attachment.content_type = size, row.content_type
        attachment.content_hash = digest or row.content_hash
        attachment.filename = path.rsplit("/", 1)[-1]
        attachment.object_deleted_at = None
    row.live_etag = None
    db.flush()
    current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
        WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
    if current is None:
        db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id, version=1,
            object_key=key, content_type=row.content_type, size_bytes=size, content_hash=digest or "",
            created_by=access.user_id))
    else:
        current.storage_key, current.content_type = key, row.content_type
        current.size_bytes, current.content_hash = size, digest or ""
        current.deleted_at, current.available = None, True
    from app.operations.audit_log import record_audit
    record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="resource.updated" if active else "resource.created", resource_type="resource",
        resource_id=row.id, metadata={"source": source})
    db.flush()
    return read_file(row)


def refresh_index(db, access, q="", offset=0, limit=100, source="", *, commit=True,
                  path_prefix="", filename=None):
    from app.files.operations import workspace_lock
    with workspace_lock(db, access.user_id):
        return _refresh_index(db, access, q, offset, limit, source, commit=commit,
            path_prefix=path_prefix, filename=filename)


def _refresh_index(db, access, q, offset, limit, source, *, commit, path_prefix="", filename=None):
    """Discover files created by shell commands from object metadata, never read bodies."""
    storage = backend(db)
    if storage is None:
        return None, False
    from app.files.workspace import valid_path, read_file
    from app.operations.audit_log import record_audit
    from app.files.quotas import lock_workspace
    root = prefix(access.user_id)
    lock_workspace(db, access.user_id)
    known_keys = dict(db.execute(select(WorkspaceFileVersion.storage_key, WorkspaceFile.path)
        .join(WorkspaceFile, WorkspaceFile.id == WorkspaceFileVersion.file_id)
        .where(WorkspaceFile.owner_user_id == access.user_id, WorkspaceFile.deleted_at.is_(None),
            WorkspaceFile.current_version == WorkspaceFileVersion.version)).all())
    directories = {} if filename else {row.path: row for row in db.scalars(select(WorkspaceDirectory).where(
        WorkspaceDirectory.owner_user_id == access.user_id))}
    marker_paths = {stored_marker_key(row): path for path, row in directories.items()}
    objects, marked_directories = {}, {}
    scan_prefix = root + path_prefix
    if path_prefix and (
        any(key.startswith(root) and key[len(root):] != path for key, path in known_keys.items())
        or any(key.startswith(root) and not key.startswith(root + path + "/") for key, path in marker_paths.items())
    ):
        scan_prefix = root
    for item in storage.iter_objects(scan_prefix):
        key = item["Key"]
        if not key.startswith(root):
            raise HTTPException(502, "资源目录返回了无效文件")
        path = known_keys.get(key, key[len(root):])
        if key in marker_paths:
            path = marker_paths[key] + "/" + DIRECTORY_MARKER
        if not path.startswith(path_prefix) or (filename and path.rsplit("/", 1)[-1].casefold() != filename.casefold()):
            continue
        if str(item.get("Type", "")).lower() == "symlink":
            continue
        if not path or any(part in EXCLUDED for part in path.split("/")):
            continue
        if path.rsplit("/", 1)[-1] == DIRECTORY_MARKER or path.endswith("/"):
            directory = marker_paths.get(key, path.rsplit("/", 1)[0])
            if directory:
                valid_path(directory)
                marked_directories[directory] = key
            continue
        valid_path(path)
        objects[path] = item
    rows = []
    for path in marked_directories.keys() - directories.keys():
        db.add(WorkspaceDirectory(account_id=access.user_id, path=path, created_by=access.user_id,
            marker_key=marked_directories[path]))
    for path, row in directories.items():
        if (path.startswith(path_prefix) and path not in marked_directories
                and stored_marker_key(row) == directory_marker_key(access.user_id, path)):
            db.delete(row)
    present = set()
    for path, item in sorted(objects.items()):
        row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
            WorkspaceFile.path == path).execution_options(populate_existing=True))
        if row and not row.deleted_at:
            current_key = db.scalar(select(WorkspaceFileVersion.storage_key).where(
                WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == row.current_version))
            if current_key and current_key != item["Key"]:
                # A different object at the mounted path cannot replace a migrated file.
                continue
        present.add(path)
        size = int(item.get("Size", item.get("size", 0)))
        if row is None:
            row = WorkspaceFile(id=uuid4(), account_id=access.user_id, path=path,
                content_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                size_bytes=size, content_hash="", current_version=1, created_by=access.user_id,
                updated_by=access.user_id, source="model_output", expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
            db.add(row)
            db.flush()
            db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id, version=1,
                object_key=item["Key"], content_type=row.content_type,
                size_bytes=size, content_hash="", created_by=access.user_id))
            record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
                action="resource.created", resource_type="resource", resource_id=row.id,
                metadata={"source": "cloud_computer"})
        elif row.deleted_at:
            for old in db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id)):
                db.delete(old)
            row.path = f"deleted/{row.id}/{uuid4().hex}"
            row.updated_by = access.user_id
            db.flush()
            row = WorkspaceFile(id=uuid4(), account_id=access.user_id, path=path,
                content_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                size_bytes=size, content_hash="", current_version=1, created_by=access.user_id,
                updated_by=access.user_id, source="model_output", expires_at=datetime(9999, 12, 31, tzinfo=timezone.utc))
            db.add(row)
            db.flush()
            db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id, version=1,
                object_key=item["Key"], content_type=row.content_type,
                size_bytes=size, content_hash="", created_by=access.user_id))
        elif row.size_bytes != size:
            row.size_bytes = size
            row.content_hash = ""
            row.updated_by = access.user_id
            current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
                WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
            if current:
                current.storage_key, current.size_bytes = item["Key"], size
                current.content_type, current.content_hash = row.content_type, ""
        etag = item.get("ETag")
        if etag and row.live_etag != etag:
            row.live_etag, row.content_hash = etag, ""
            row.updated_at, row.updated_by = now(), access.user_id
            current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
                WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
            if current:
                current.storage_key, current.content_hash = item["Key"], ""
                current.size_bytes, current.content_type = size, row.content_type
        if q.lower() in path.lower() and (not source or row.source == source):
            rows.append(row)
    missing_query = select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(path_prefix, autoescape=True))
    if filename:
        lower_path = func.lower(WorkspaceFile.path)
        missing_query = missing_query.where((lower_path == filename.lower()) |
            lower_path.endswith("/" + filename.lower(), autoescape=True))
    for row in db.scalars(missing_query):
        key = db.scalar(select(WorkspaceFileVersion.storage_key).where(
            WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == row.current_version))
        if row.path in present:
            continue
        if key and key != object_key(access.user_id, row.path):
            if q.lower() in row.path.lower() and (not source or row.source == source):
                rows.append(row)
            continue
        row.deleted_at = now()
        for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id)):
            attachment.deleted_at = row.deleted_at
            attachment.object_deleted_at = row.deleted_at
    if commit:
        db.commit()
    else:
        db.flush()
    rows.sort(key=lambda row: row.path)
    end = None if limit is None else offset + limit
    return [read_file(row) for row in rows[offset:end]], end is not None and len(rows) > end


def remove_live(db, resource):
    storage = backend(db)
    if storage is not None:
        from app.files.workspace import _current_file_version
        storage.delete(_current_file_version(db, resource).storage_key)
    resource.live_etag = None
    return True


def delete_path(db, access, resource, *, remove_object=True, commit=True):
    from app.files.quotas import lock_workspace
    from app.identity.auth import now as current_time
    if resource.owner_user_id != access.user_id:
        raise HTTPException(404, "资源不存在")
    lock_workspace(db, access.user_id)
    storage = backend(db)
    if storage is None:
        raise HTTPException(503, "资源目录尚未就绪")
    if remove_object:
        remove_live(db, resource)
    resource.deleted_at = current_time()
    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id)):
        attachment.deleted_at = resource.deleted_at
        attachment.object_deleted_at = resource.deleted_at
    from app.operations.audit_log import record_audit
    record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="resource.deleted", resource_type="resource", resource_id=resource.id,
        metadata={"source": "workspace_mount"})
    if commit:
        db.commit()


def register_operation(db, access, operation, value):
    """Update the metadata index after a direct mounted file operation; never copy bytes."""
    from app.files.workspace import read_file
    name, path = operation["name"], operation.get("path", "")
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
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
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.path == path))
    return read_file(row) if row else result


def refresh_current_metadata(db, access, resource, *, metadata=None):
    """Revalidate current mounted object metadata before a server-side image read."""
    from app.files.quotas import lock_workspace
    from app.files.workspace import _current_file_version, owned
    access = access.revalidate(db) if access.session_hash else access
    resource = owned(db, access, access.user_id, resource.id)
    storage = backend(db)
    if storage is None and metadata is None:
        return resource
    lock_workspace(db, access.user_id)
    current = _current_file_version(db, resource)
    key = current.storage_key
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
        current.size_bytes, current.content_hash = size, ""
        for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id,
                ChatAttachment.deleted_at.is_(None))):
            attachment.size_bytes, attachment.content_hash, attachment.object_key = size, "", key
            attachment.text_content = None
            attachment.width = attachment.height = None
        db.commit()
    return resource
