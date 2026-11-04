"""Bounded, cross-process cache outside isolated execution containers."""
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import time

from filelock import FileLock
from fastapi import HTTPException

from .config import get_settings
from . import storage_billing as billing
from .storage import download_range_fileobj

logger = logging.getLogger(__name__)


def prune_cache(extra=0):
    config = get_settings()
    root = Path(config.resource_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "eviction.lock"), timeout=120):
        entries = sorted(root.glob("*.bin"), key=lambda item: item.stat().st_mtime)
        total = sum(item.stat().st_size for item in entries)
        for entry in entries:
            if time.time() - entry.stat().st_mtime > config.resource_cache_idle_seconds or total + extra > config.resource_cache_max_bytes:
                total -= entry.stat().st_size
                entry.unlink(missing_ok=True)
                entry.with_suffix(".sha256").unlink(missing_ok=True)
        for entry in root.glob("*.part"):
            if time.time() - entry.stat().st_mtime > config.resource_cache_idle_seconds:
                entry.unlink(missing_ok=True)


def cached(root, key, length, digest=None):
    target = root / f"{key}.bin"
    with FileLock(str(root / "eviction.lock"), timeout=120):
        if not target.exists():
            return None
        data = target.read_bytes()
        checksum = target.with_suffix(".sha256")
        expected = digest or (checksum.read_text() if checksum.exists() else None)
        if (len(data) == length and hashlib.sha256(data).hexdigest() == expected
                and time.time() - target.stat().st_mtime <= get_settings().resource_cache_idle_seconds):
            os.utime(target, None)
            return data
        target.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        return None


def read_bytes(db, access, resource, version, *, offset=0, limit=None):
    """Revalidate before cache access and meter only bytes actually fetched from storage."""
    from .workspace_files import owned
    if access.session_hash:
        access = access.revalidate(db)
    owned(db, access, access.workspace_id, resource.id)
    config = get_settings()
    root = Path(config.resource_cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    length = min(version.size_bytes - offset, limit if limit is not None else version.size_bytes)
    if offset < 0 or length < 0:
        raise HTTPException(422, "读取范围无效")
    identity = f"{access.workspace_id}:{resource.id}:{version.version}:{version.content_hash}"
    key = hashlib.sha256(f"{identity}:{offset}:{length}".encode()).hexdigest()
    full_key = hashlib.sha256(f"{identity}:0:{version.size_bytes}".encode()).hexdigest()
    # A bounded lock stripe serializes all ranges of a resource version across API processes.
    stripe = hashlib.sha256(identity.encode()).hexdigest()[:2]
    with FileLock(str(root / f"{stripe}.lock"), timeout=120):
        if access.session_hash: access = access.revalidate(db)
        db.refresh(resource)
        owned(db, access, access.workspace_id, resource.id)
        db.refresh(version)
        if version.file_id != resource.id or version.deleted_at or version.available is False:
            raise HTTPException(404, "资源版本已删除或不可用")
        full = cached(root, full_key, version.size_bytes, version.content_hash)
        if full is not None:
            return full[offset:offset + length]
        data = cached(root, key, length)
        if data is not None:
            return data
        transfer = billing.reserve_download(db, access, length,
            f"model_read:{resource.id}:{version.version}")
        db.commit()
        data = bytearray()
        try:
            if length:
                with download_range_fileobj(version.object_key, offset, offset + length - 1) as source:
                    while len(data) < length:
                        block = source.read(min(1024 * 1024, length - len(data)))
                        if not block:
                            raise IOError("资源内容不完整")
                        billing.transfer_checkpoint(db, transfer, preparing=len(block))
                        billing.transfer_checkpoint(db, transfer, sent=len(block))
                        db.commit()
                        data.extend(block)
            if offset == 0 and length == version.size_bytes and hashlib.sha256(data).hexdigest() != version.content_hash:
                raise IOError("资源摘要校验失败")
            billing.transfer_checkpoint(db, transfer, finish="completed")
            if offset == 0 and length == version.size_bytes:
                version.available = True
            db.commit()
        except BaseException:
            db.rollback()
            billing.transfer_checkpoint(db, transfer, finish="failed")
            db.commit()
            raise
        try:
            store_bytes(root, key, data)
        except OSError:
            # Cache failure must not replay a successfully billed object transfer.
            logger.warning("Resource cache write failed", exc_info=True)
        return bytes(data)


def store_bytes(root, key, data):
    length = len(data)
    target = root / f"{key}.bin"
    if length <= get_settings().resource_cache_max_bytes:
        prune_cache(length)
        with FileLock(str(root / "eviction.lock"), timeout=120):
            # Recheck space while holding the global lock: another version may have completed.
            total = sum(item.stat().st_size for item in root.glob("*.bin"))
            if total + length > get_settings().resource_cache_max_bytes:
                return
            handle, temporary = tempfile.mkstemp(dir=root, suffix=".part")
            try:
                with os.fdopen(handle, "wb") as output:
                    output.write(data)
                os.replace(temporary, target)
                target.with_suffix(".sha256").write_text(hashlib.sha256(data).hexdigest())
            finally:
                Path(temporary).unlink(missing_ok=True)
