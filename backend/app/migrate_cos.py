"""One-time verified object migration: python -m app.migrate_cos [--execute --version N]."""
import argparse
import hashlib
from tempfile import TemporaryFile

from sqlalchemy import func, select

from app.infra.config import get_settings
from app.infra.cos_storage import CosStorage
from app.infra.db import SessionLocal
from .models import Document, DocumentUpload, SkillUpload, IngestionJob, JobState
from app.infra.storage import client
from app.files.settings import locked
from app.operations.site_audit import record


def migrate(db, *, execute=False, version=None, legacy=None):
    from app.infra.storage_backend import active_settings
    active = active_settings(db)
    if active and active.provider != "cos":
        raise ValueError("当前使用其他供应商，请使用 migrate_storage 进行校验迁移")
    row = locked(db)
    if not row.bucket or row.tested_version != row.version:
        raise ValueError("请先在后台保存并通过存储连接测试")
    if version is not None and row.version != version:
        raise ValueError("配置版本已变化，请重新检查后迁移")
    documents = db.scalars(select(Document).order_by(Document.id).with_for_update()).all()
    summary = {"documents": len(documents), "bytes": sum(d.size_bytes for d in documents),
        "version": row.version, "active": row.active}
    if not execute:
        return summary
    if version is None:
        raise ValueError("执行迁移必须指定已核对的 --version")
    if row.active:
        return {**summary, "verified": True}
    if db.scalar(select(func.count()).select_from(IngestionJob).where(IngestionJob.state == JobState.RUNNING)):
        raise ValueError("仍有文档处理任务运行，请先排空任务")
    if db.scalar(select(func.count()).select_from(DocumentUpload).where(
            DocumentUpload.state.in_(["uploading", "completing", "verifying"]))):
        raise ValueError("仍有未完成上传，请先暂停并排空")
    if db.scalar(select(func.count()).select_from(SkillUpload).where(
            SkillUpload.state.not_in(["cleaned", "cancelled"]))):
        raise ValueError("仍有待处理的 Skill 上传，请先完成或取消")
    source = legacy or client()
    target = CosStorage(row)
    for document in documents:
        with TemporaryFile() as stream:
            source.download_fileobj(get_settings().s3_bucket, document.object_key, stream)
            stream.seek(0)
            size, digest = fingerprint(stream)
            if size != document.size_bytes or digest != document.content_hash:
                raise ValueError("原文件摘要或大小不符，迁移未切换")
            stream.seek(0)
            target.put(document.object_key, stream, document.content_type or "application/octet-stream")
        with target.download(document.object_key) as stream:
            if fingerprint(stream) != (size, digest):
                raise ValueError("COS 文件校验失败，迁移未切换")
    row.active = True
    record(db, None, "storage.migrated", "storage", 1,
        details={"version": row.version, "count": len(documents), "accepted": True})
    db.commit()
    return {**summary, "active": True, "verified": True}


def fingerprint(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="暂停新上传、排空处理任务后，校验复制原文件并启用 COS")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--version", type=int)
    args = parser.parse_args()
    try:
        with SessionLocal() as session:
            print(migrate(session, execute=args.execute, version=args.version))
    except Exception:
        # SDK/database exceptions may contain secrets or signed URLs.
        raise SystemExit("迁移未完成，未切换存储。请核对配置、任务状态及源文件摘要；原文件保持不变。") from None
