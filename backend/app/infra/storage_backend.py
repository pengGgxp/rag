"""Single active object store, with no fallback when an active store fails."""
from fastapi import HTTPException
from sqlalchemy import select

from app.infra.cos_storage import CosStorage
from app.infra.oss_storage import OssStorage
from app.models import SiteStorageSettings

PROVIDER_IDS = {"cos": 1, "oss": 2}


def configured_backend(settings):
    if settings.provider == "oss":
        return OssStorage(settings)
    if settings.provider == "cos":
        return CosStorage(settings)
    raise HTTPException(503, "文件存储配置无效，请联系站点管理员")


def active_settings(db):
    return db.scalar(select(SiteStorageSettings).where(SiteStorageSettings.active.is_(True))
        .execution_options(populate_existing=True))


def current_storage(db, *, require_active=True):
    row = active_settings(db)
    if not row:
        if not require_active:
            return None
        raise HTTPException(503, "文件存储尚未就绪，请联系站点管理员")
    return configured_backend(row)
