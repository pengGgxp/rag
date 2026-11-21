"""Offline, verified migration of current resources to the mounted directory."""
import argparse

from sqlalchemy import select

from .db import SessionLocal
from .migrate_cos import fingerprint
from .models import ChatAttachment, Document, WorkspaceFile, WorkspaceFileVersion
from .shared_resources import object_key
from . import storage_backend


def plan(db):
    items, targets = [], set()
    for resource in db.scalars(select(WorkspaceFile).where(WorkspaceFile.deleted_at.is_(None))):
        versions = list(db.scalars(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == resource.id)))
        current = next((v for v in versions if v.version == resource.current_version and not v.deleted_at), None)
        attachments = list(db.scalars(select(ChatAttachment).where(
            ChatAttachment.resource_file_id == resource.id, ChatAttachment.deleted_at.is_(None))))
        target = object_key(resource.account_id, resource.path)
        if target in targets:
            raise ValueError(f"资源路径重复: {resource.id}")
        targets.add(target)
        if len(attachments) > 1:
            raise ValueError(f"资源存在多个附件索引，需先合并关联: {resource.id}")
        if db.scalar(select(Document.id).where(Document.object_key == target).limit(1)):
            raise ValueError(f"资源目标与知识库原文件冲突: {resource.id}")
        source = current.object_key if current else next((a.object_key for a in attachments), target if resource.live_etag else None)
        if not source:
            raise ValueError(f"资源缺少当前文件对象: {resource.id}")
        items.append(dict(resource=resource, versions=versions, current=current,
                          attachments=attachments, source=source, target=target))
    return items


def _fingerprint(storage, key):
    with storage.download(key) as stream:
        return fingerprint(stream)


def migrate(db, *, execute=False, offline=False):
    if execute and not offline:
        raise ValueError("执行前请停止 API、Worker、清理任务和云电脑，并传入 --offline")
    settings = storage_backend.active_settings(db)
    if not settings or settings.provider != "oss":
        raise ValueError("资源目录挂载要求阿里云 OSS 为当前主存储")
    storage = storage_backend.current_storage(db)
    items = plan(db)
    # Complete preflight before copying objects or changing database pointers.
    for item in items:
        source, target = item["source"], item["target"]
        head = storage.head(source)
        if not head:
            raise ValueError(f"当前资源对象缺失: {item['resource'].id}")
        size, digest = _fingerprint(storage, source)
        resource = item["resource"]
        if source != target and (size != resource.size_bytes or
                (resource.content_hash and digest != resource.content_hash)):
            raise ValueError(f"当前资源摘要不符: {resource.id}")
        if source != target and storage.head(target) is not None:
            if _fingerprint(storage, target) != (size, digest):
                raise ValueError(f"挂载目标已有不同内容，未覆盖: {resource.id}")
        item.update(size=size, digest=digest, etag=head.get("ETag"))
    summary = {"resources": len(items), "verified": len(items),
        "already_mounted": sum(i["source"] == i["target"] for i in items),
        "bytes": sum(i["size"] for i in items)}
    if not execute:
        return summary
    for item in items:
        if item["source"] != item["target"] and storage.head(item["target"]) is None:
            storage.copy(item["source"], item["target"], etag=item["etag"])
        if _fingerprint(storage, item["target"]) != (item["size"], item["digest"]):
            raise ValueError(f"迁移目标校验失败: {item['resource'].id}")
    # Originals remain offline rollback backups; runtime exposes only current files.
    try:
        for item in items:
            resource, current = item["resource"], item["current"]
            for version in item["versions"]:
                if version is not current:
                    db.delete(version)
            db.flush()
            if current is None:
                current = WorkspaceFileVersion(file_id=resource.id, account_id=resource.account_id,
                    version=1, object_key=item["target"], content_type=resource.content_type,
                    size_bytes=item["size"], content_hash=item["digest"], created_by=resource.created_by)
                db.add(current)
            current.version, current.object_key = 1, item["target"]
            current.size_bytes, current.content_hash = item["size"], item["digest"]
            current.available, current.deleted_at = True, None
            resource.current_version, resource.size_bytes, resource.content_hash = 1, item["size"], item["digest"]
            resource.live_etag = storage.head(item["target"]).get("ETag")
            for attachment in item["attachments"]:
                attachment.object_key, attachment.resource_version = item["target"], 1
                attachment.size_bytes, attachment.content_hash = item["size"], item["digest"]
                attachment.content_type, attachment.resource_path = resource.content_type, resource.path
                attachment.text_content = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**summary, "originals_retained_for_rollback": True}


def main():
    parser = argparse.ArgumentParser(description="迁移当前资源到工作区共享挂载目录")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        print(migrate(db, execute=args.execute, offline=args.offline))


if __name__ == "__main__":
    main()
