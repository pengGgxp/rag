"""Persistent workspace directory; immutable versions are outside the writable mount."""
import hashlib
import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from .auth import now
from .models import ChatAttachment, WorkspaceFile, WorkspaceFileVersion

EXCLUDED = {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"}


def prefix(workspace_id):
    return f"workspaces/{workspace_id}/resource-directory/"


def backend(db):
    from .storage_backend import active_settings, current_storage
    settings = active_settings(db)
    return current_storage(db) if settings and settings.provider == "oss" else None


def etag(head):
    return head.get("ETag") if head else None


def same_content(left, right):
    if not left or not right or int(left["Content-Length"]) != int(right["Content-Length"]):
        return False
    if left.get("CRC64") is not None and right.get("CRC64") is not None:
        return left["CRC64"] == right["CRC64"]
    return etag(left) == etag(right)


def publish(db, resource, version):
    """Publish uploads/edits into the same directory observed by every VM."""
    storage = backend(db)
    if storage is None:
        return
    key = prefix(resource.workspace_id) + resource.path
    current = storage.head(key)
    source = storage.head(version.object_key)
    if not source or int(source["Content-Length"]) != version.size_bytes:
        raise HTTPException(409, "资源文件尚未保存完整，请重试")
    if current and etag(current) != resource.live_etag and not same_content(current, source):
        raise HTTPException(409, "云电脑正在修改该文件，请等待完成后重新读取")
    if not same_content(current, source):
        storage.copy(version.object_key, key, etag=etag(source))
        current = storage.head(key)
    if not same_content(current, source):
        raise HTTPException(409, "文件已变化，请重新读取后再保存")
    resource.live_etag = etag(current)


def remove_live(db, resource):
    storage = backend(db)
    if storage is not None:
        storage.delete(prefix(resource.workspace_id) + resource.path)
    resource.live_etag = None


def migrate(db):
    """One-time, resumable server-side publication; never download or meter bytes."""
    storage = backend(db)
    if storage is None:
        raise RuntimeError("shared directory requires active OSS")
    count = 0
    identifiers = list(db.scalars(select(WorkspaceFile.id).where(
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.live_etag.is_(None))))
    from .storage_billing import lock_workspace
    for identifier in identifiers:
        resource = db.get(WorkspaceFile, identifier, populate_existing=True)
        lock_workspace(db, resource.workspace_id)
        db.refresh(resource)
        if resource.deleted_at or resource.live_etag:
            db.commit()
            continue
        version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == resource.id,
            WorkspaceFileVersion.version == resource.current_version))
        if not version or version.available is False or version.deleted_at:
            db.commit()
            continue
        if version.available is None and storage.head(version.object_key) is None:
            version.available = False
            db.commit()
            continue
        publish(db, resource, version)
        db.commit()
        count += 1
    return count


def objects(storage, workspace_id):
    root = prefix(workspace_id)
    result = {}
    from .workspace_files import valid_path
    for item in storage.iter_objects(root):
        if not item["Key"].startswith(root):
            raise RuntimeError("resource object escaped workspace prefix")
        path = item["Key"][len(root):]
        if not path or path.endswith("/"):
            continue
        valid_path(path)
        if any(part in EXCLUDED for part in path.split("/")):
            continue
        result[path] = item
    return result


def snapshot(db, access, storage, entries, before):
    """Register only changes, using OSS CopyObject instead of VM file transfers."""
    from .audit_log import record_audit
    from .storage_billing import check_capacity, lock_workspace
    from .workspace_files import MAX_BYTES, read_file, valid_path
    access.editor()
    lock_workspace(db, access.workspace_id)
    changed = []
    try:
        for path, entry in entries.items():
            valid_path(path)
            if any(part in EXCLUDED for part in path.split("/")):
                continue
            if entry["size"] > MAX_BYTES:
                raise HTTPException(413, "文件不能超过 200 MiB")
            key = prefix(access.workspace_id) + path
            head = storage.head(key)
            if not head or path not in before or etag(head) != before[path]["ETag"]:
                raise HTTPException(409, "共享文件正在变化，请稍后重试登记文件")
            if int(head["Content-Length"]) != entry["size"]:
                raise HTTPException(409, "文件仍在写入，请稍后重试登记文件")
            resource = db.scalar(select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.path == path)
                .execution_options(populate_existing=True))
            if resource and resource.deleted_at:
                # Recreating a filename must not revive deleted message references.
                resource.path = f"deleted/{resource.id}/{uuid4().hex}"
                db.flush()
                resource = None
            if resource and resource.content_hash == entry["sha256"]:
                resource.live_etag = etag(head)
                continue
            check_capacity(db, access.workspace_id, entry["size"])
            active = resource is not None
            if resource is None:
                resource = WorkspaceFile(id=uuid4(), workspace_id=access.workspace_id,
                    path=path, current_version=0, created_by=access.user_id,
                    updated_by=access.user_id, size_bytes=0, content_hash="",
                    content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
                db.add(resource)
            version_number = resource.current_version + 1
            archive = f"workspaces/{access.workspace_id}/files/{resource.id}/{version_number}/{path}"
            storage.copy(key, archive, etag=etag(head))
            copied = storage.head(archive)
            if not same_content(copied, head) or etag(storage.head(key)) != etag(head):
                raise HTTPException(409, "共享文件已变化，请稍后重试登记文件")
            resource.current_version = version_number
            resource.size_bytes, resource.content_hash = entry["size"], entry["sha256"]
            resource.live_etag, resource.updated_by = etag(head), access.user_id
            resource.expires_at = datetime(9999, 12, 31, tzinfo=timezone.utc)
            db.flush()
            db.add(WorkspaceFileVersion(file_id=resource.id, workspace_id=access.workspace_id,
                version=version_number, object_key=archive, content_type=resource.content_type,
                size_bytes=entry["size"], content_hash=entry["sha256"], created_by=access.user_id))
            record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
                action="resource.updated" if active else "resource.created", resource_type="resource",
                resource_id=resource.id, metadata={"version": version_number, "source": "cloud_computer"})
            db.flush()
            changed.append(read_file(resource))
        for resource in db.scalars(select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.deleted_at.is_(None),
                WorkspaceFile.live_etag.is_not(None))):
            if resource.path in entries or any(part in EXCLUDED for part in resource.path.split("/")):
                continue
            key = prefix(access.workspace_id) + resource.path
            if storage.head(key) is not None:
                continue
            resource.deleted_at, resource.live_etag = now(), None
            for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == resource.id)):
                attachment.deleted_at = resource.deleted_at
            record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
                action="resource.deleted", resource_type="resource", resource_id=resource.id,
                metadata={"source": "cloud_computer"})
        db.commit()
        return changed
    except BaseException:
        db.rollback()
        raise


def recover(db, access, storage):
    """VM disappeared: hash only unindexed changes; never replay a command."""
    before = objects(storage, access.workspace_id)
    entries = {}
    for path, item in before.items():
        resource = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))
        if resource and resource.live_etag == item["ETag"]:
            digest = resource.content_hash
        else:
            if item["Size"] > 200 * 1024 * 1024:
                raise HTTPException(413, "文件过大，请在云电脑中缩小后重试")
            hasher = hashlib.sha256()
            with storage.download(item["Key"]) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()
        entries[path] = {"sha256": digest, "size": item["Size"]}
    return snapshot(db, access, storage, entries, before)
