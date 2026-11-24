"""在已配置的对象存储之间离线迁移，支持校验和续跑。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text

from app.infra.db import SessionLocal
from ..models import (
    ChatAttachment, CloudComputerCommand, CloudComputerSession, Conversation, Document,
    DocumentUpload, SkillUpload, ImageTask, IngestionJob, JobState, SiteStorageSettings,
    StorageMigration, StorageMigrationObject, WorkspaceExecutionJob, WorkspaceFileVersion, WorkspaceFile,
)
from app.files.integrity import stream_fingerprint as fingerprint
from app.operations.site_audit import record
from app.infra.storage_backend import PROVIDER_IDS, active_settings, configured_backend


def manifest(db):
    """Database references, not bucket contents, define retained objects."""
    objects = {}

    def add(row):
        item = {"object_key": row.object_key, "size_bytes": row.size_bytes,
            "content_hash": row.content_hash, "content_type": row.content_type or "application/octet-stream"}
        previous = objects.get(row.object_key)
        if previous and (previous["size_bytes"], previous["content_hash"]) != (row.size_bytes, row.content_hash):
            raise ValueError("同一对象的文件记录摘要不一致，请先修复数据")
        objects.setdefault(row.object_key, item)

    for row in db.scalars(select(Document)):
        add(row)
    for row in db.scalars(select(WorkspaceFileVersion).where(
            WorkspaceFileVersion.deleted_at.is_(None), WorkspaceFileVersion.available.is_not(False))):
        add(row)
    for row in db.scalars(select(ChatAttachment).where(ChatAttachment.object_deleted_at.is_(None))):
        add(row)
    from app.files.shared import prefix
    from types import SimpleNamespace
    for row in db.scalars(select(WorkspaceFile).where(WorkspaceFile.deleted_at.is_(None),
            WorkspaceFile.live_etag.is_not(None))):
        add(SimpleNamespace(object_key=prefix(row.account_id)+row.path, size_bytes=row.size_bytes,
            content_hash=row.content_hash, content_type=row.content_type))
    return dict(sorted(objects.items()))


def assert_drained(db):
    checks = [
        select(DocumentUpload.id).where(DocumentUpload.state.in_(("uploading", "completing", "verifying"))),
        select(SkillUpload.id).where(SkillUpload.state.not_in(("cleaned", "cancelled"))),
        select(IngestionJob.id).where(IngestionJob.state.in_((JobState.QUEUED, JobState.RUNNING))),
        select(Conversation.id).where(Conversation.active_request_id.is_not(None)),
        select(CloudComputerSession.id).where(CloudComputerSession.ended_at.is_(None)),
        select(CloudComputerCommand.id).where(CloudComputerCommand.status.in_(
            ("queued", "preparing", "running", "syncing", "save_queued"))),
        select(WorkspaceExecutionJob.id).where(WorkspaceExecutionJob.status == "running"),
        select(ImageTask.id).where(ImageTask.status.not_in(("completed", "partial", "failed", "cancelled"))),
    ]
    if any(db.scalar(check.limit(1)) for check in checks):
        raise ValueError("仍有上传、对话、图片、解析或云电脑任务，请先排空再迁移")


@contextmanager
def migration_lock(db):
    # Session-level lock survives per-object commits and serializes config saves.
    connection = db.connection()
    postgres = connection.dialect.name == "postgresql"
    if postgres and not connection.scalar(text("SELECT pg_try_advisory_lock(3020046)")):
        raise ValueError("已有存储配置或迁移操作正在执行")
    try:
        yield
    finally:
        if postgres:
            db.execute(text("SELECT pg_advisory_unlock(3020046)"))
            db.commit()


def migrate(db, *, target_provider, version, execute=False, offline=False, resume=None, factory=configured_backend):
    target = db.get(SiteStorageSettings, PROVIDER_IDS[target_provider], populate_existing=True)
    if not target or target.version != version or target.tested_version != version:
        raise ValueError("请先保存并测试目标存储，核对配置版本")
    source = active_settings(db)
    items = manifest(db)
    summary = {"source": source.provider if source else None, "target": target_provider,
        "objects": len(items), "bytes": sum(item["size_bytes"] for item in items.values()),
        "target_version": version, "already_active": bool(target.active)}
    if not execute:
        return summary
    if target.active:
        return {**summary, "verified": True, "changed": False}
    if not offline:
        raise ValueError("执行迁移前必须停止 API、Worker 和清理进程，并传入 --offline")
    assert_drained(db)
    if not source and items:
        raise ValueError("存在文件但没有有效主存储，请先核对原存储，不能直接切换")
    source_version = source.version if source else 0
    run = db.get(StorageMigration, UUID(str(resume))) if resume else None
    if resume and (not run or run.status == "completed" or run.source_provider != (summary["source"] or "none")
            or run.target_provider != target_provider or run.source_version != source_version
            or run.target_version != version):
        raise ValueError("迁移记录与当前存储配置不符，请重新预检")
    if not run:
        run = StorageMigration(source_provider=summary["source"] or "none", target_provider=target_provider,
            source_version=source_version, target_version=version)
        db.add(run)
        db.flush()
        db.add_all(StorageMigrationObject(migration_id=run.id, **item) for item in items.values())
        db.commit()
    rows = db.scalars(select(StorageMigrationObject).where(
        StorageMigrationObject.migration_id == run.id).order_by(StorageMigrationObject.object_key)).all()
    expected = {row.object_key: {name: getattr(row, name) for name in (
        "object_key", "size_bytes", "content_hash", "content_type")} for row in rows}
    if expected != items:
        raise ValueError("迁移期间文件清单发生变化，请保持服务停止并重新预检")
    run_id, source_id, target_id = run.id, source.id if source else None, target.id
    try:
        source_store = factory(source) if source else None
        target_store = factory(target)
        run.status, run.error = "copying", None
        db.commit()
        for row in rows:
            expected_hash = (row.size_bytes, row.content_hash)
            if target_store.head(row.object_key) is not None:
                with target_store.download(row.object_key) as stream:
                    if fingerprint(stream) != expected_hash:
                        raise ValueError("目标已有对象与原文件不同，未覆盖且未切换")
            else:
                with source_store.download(row.object_key) as stream:
                    if fingerprint(stream) != expected_hash:
                        raise ValueError("原文件缺失或摘要不一致，迁移未切换")
                    stream.seek(0)
                    target_store.put(row.object_key, stream, row.content_type)
                with target_store.download(row.object_key) as stream:
                    if fingerprint(stream) != expected_hash:
                        raise ValueError("目标文件校验失败，迁移未切换")
            row.verified = True
            db.commit()
        run.status = "verifying"
        db.commit()
        # Verify source even when resuming an object already present at the target.
        for row in rows:
            with source_store.download(row.object_key) as stream:
                if fingerprint(stream) != (row.size_bytes, row.content_hash):
                    raise ValueError("最终源文件校验失败，迁移未切换")
            with target_store.download(row.object_key) as stream:
                if fingerprint(stream) != (row.size_bytes, row.content_hash):
                    raise ValueError("最终目标文件校验失败，迁移未切换")
        if manifest(db) != items:
            raise ValueError("最终文件清单变化，迁移未切换")
        source = db.get(SiteStorageSettings, source_id, populate_existing=True) if source_id else None
        target = db.get(SiteStorageSettings, target_id, populate_existing=True)
        current = active_settings(db)
        if ((source and (source.version != source_version or not current or current.id != source_id))
                or (not source and current) or target.version != version or target.tested_version != version):
            raise ValueError("存储配置已变化，迁移未切换")
        if source:
            source.active = False
            source.version += 1
            source.tested_version = source.version
            db.flush()
        target.active = True
        target.version += 1
        target.tested_version = target.version
        run.status, run.completed_at = "completed", datetime.now(timezone.utc)
        record(db, None, "storage.migrated", "storage", target.id,
            details={**summary, "migration_id": str(run_id), "accepted": True})
        db.commit()
        return {**summary, "migration_id": str(run_id), "verified": True, "changed": True}
    except Exception:
        db.rollback()
        run = db.get(StorageMigration, run_id)
        run.status, run.error = "failed", "迁移未完成，原主存储保持不变；请核对文件及权限后恢复迁移"
        db.commit()
        raise ValueError(f"迁移未完成，未切换存储。可使用 --resume {run_id} 恢复；请核对文件、权限和配置") from None


def main():
    parser = argparse.ArgumentParser(description="校验复制数据库引用文件并切换主存储；执行时必须停止业务进程")
    parser.add_argument("--target", choices=tuple(PROVIDER_IDS), required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--resume", type=UUID)
    args = parser.parse_args()
    # Keep a dedicated connection checked out across per-object commits, so the
    # PostgreSQL session advisory lock cannot be returned to the pool early.
    from app.infra.db import engine
    from sqlalchemy.orm import Session
    try:
        with engine.connect() as connection, Session(bind=connection) as db, migration_lock(db):
            print(migrate(db, target_provider=args.target, version=args.version,
                execute=args.execute, offline=args.offline, resume=args.resume))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("迁移未完成，未切换存储。请检查文件、存储权限和配置；原文件保留。") from None


if __name__ == "__main__":
    main()
