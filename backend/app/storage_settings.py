"""Encrypted independent object-store connections without credential disclosure."""
import hashlib
import io
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import text

from .db import get_db
from .models import SiteStorageSettings
from .site_auth import protect, reveal, site_admin
from .site_audit import record
from .cos_storage import CosStorage
from .oss_storage import OssStorage
from .storage_backend import PROVIDER_IDS, active_settings

router = APIRouter(prefix="/site/admin/storage", tags=["site-admin"])


def locked(db, provider="cos"):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020046)"))
    identifier = PROVIDER_IDS[provider]
    row = db.get(SiteStorageSettings, identifier, with_for_update=True, populate_existing=True)
    if row is None:
        row = SiteStorageSettings(id=identifier, provider=provider, version=0, bucket="", region="", active=False)
        db.add(row)
        db.flush()
    return row


def read(row, provider="cos"):
    identifier = reveal(row.encrypted_secret_id) if row and row.encrypted_secret_id else ""
    provider = row.provider if row else provider
    return {"provider": provider, "version": row.version if row else 0,
        "bucket": row.bucket if row else "", "region": row.region if row else "cn-hangzhou" if provider == "oss" else "ap-guangzhou", "has_secret_id": bool(identifier),
        "secret_id_masked": f"{identifier[:4]}…{identifier[-4:]}" if len(identifier) > 8 else "已配置" if identifier else "",
        "has_secret_key": bool(row and row.encrypted_secret_key), "active": bool(row and row.active),
        "tested": bool(row and row.tested_version == row.version), "test_result": row.test_result if row else None}


class StorageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    provider: Literal["cos", "oss"] = "cos"
    bucket: str = Field(min_length=1, max_length=100)
    region: str = Field(pattern=r"^[a-z]{2}(?:-[a-z0-9]+){1,3}$", max_length=60)
    secret_id: SecretStr | None = None
    secret_key: SecretStr | None = None
    clear_credentials: bool = False

    @model_validator(mode="after")
    def validate_location(self):
        pattern = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$" if self.provider == "oss" else r"^[a-z0-9][a-z0-9-]{0,61}-[0-9]+$"
        if not re.fullmatch(pattern, self.bucket):
            raise ValueError("存储桶名称不符合供应商规则")
        if self.provider == "oss" and not re.fullmatch(r"(?:cn|ap|us|eu|me|na|sa|af)-[a-z0-9]+(?:-[a-z0-9]+)*", self.region):
            raise ValueError("请输入 OSS 地域，例如 cn-hangzhou")
        return self


class TestInput(BaseModel):
    version: int = Field(ge=1)
    provider: Literal["cos", "oss"] = "cos"


@router.get("")
def get_settings(db=Depends(get_db), actor=Depends(site_admin)):
    active = active_settings(db)
    return {"active_provider": active.provider if active else None,
        "connections": [read(db.get(SiteStorageSettings, identifier), provider)
            for provider, identifier in PROVIDER_IDS.items()]}


@router.put("")
def save_settings(payload: StorageInput, db=Depends(get_db), actor=Depends(site_admin)):
    row = locked(db, payload.provider)
    if row.version != payload.version:
        raise HTTPException(409, "存储配置已更新，请重新加载后保存")
    if row.active and (payload.bucket != row.bucket or payload.region != row.region):
        raise HTTPException(409, "已启用的存储位置不能直接修改，请先完成文件迁移")
    if payload.clear_credentials and row.active:
        raise HTTPException(409, "存储正在使用，不能清除凭据；请填写新凭据完成轮换")
    credentials_changed = False
    row.bucket, row.region = payload.bucket, payload.region
    if payload.clear_credentials:
        row.encrypted_secret_id = row.encrypted_secret_key = None
    else:
        for name in ("secret_id", "secret_key"):
            secret = getattr(payload, name)
            if secret and secret.get_secret_value().strip():
                setattr(row, f"encrypted_{name}", protect(secret.get_secret_value().strip()))
                credentials_changed = True
    row.version += 1
    row.test_result = None
    if row.active and credentials_changed:
        if not check_connection(row):
            db.rollback()
            record(db, actor, "storage.updated", "storage", row.id, result="failure",
                details={"fields": sorted(payload.model_fields_set), "accepted": False})
            db.commit()
            raise HTTPException(503, "新凭据验证失败，原存储配置未改变")
        row.tested_version = row.version
        row.test_result = "连接及文件操作验证通过"
    record(db, actor, "storage.updated", "storage", row.id, details={"provider": row.provider, "fields": sorted(payload.model_fields_set), "version": row.version})
    db.commit()
    return read(row)


def check_connection(row):
    storage = None
    key, multipart_id = f"_checks/{uuid4()}", None
    error = False
    try:
        storage = OssStorage(row) if row.provider == "oss" else CosStorage(row)
        body = b"zhiku-storage-check"
        storage.put(key, io.BytesIO(body))
        with storage.download(key) as downloaded:
            if hashlib.sha256(downloaded.read()).digest() != hashlib.sha256(body).digest():
                raise ValueError("Object verification failed")
        multipart_id = storage.begin(key + "-multipart", "application/octet-stream")
        etag = storage.upload_part(key + "-multipart", multipart_id, 1, body)
        storage.parts(key + "-multipart", multipart_id)
        storage.complete(key + "-multipart", multipart_id, [{"PartNumber": 1, "ETag": etag}])
        multipart_id = None
        with storage.download(key + "-multipart") as downloaded:
            if downloaded.read() != body:
                raise ValueError("Multipart verification failed")
        if int(storage.head(key)["Content-Length"]) != len(body):
            raise ValueError("Object size verification failed")
        with storage.download_range(key, 0, 3) as downloaded:
            if downloaded.read() != body[:4]:
                raise ValueError("Range verification failed")
        multipart_id = storage.begin(key + "-multipart", "application/octet-stream")
        storage.abort(key + "-multipart", multipart_id)
        multipart_id = None
        next(iter(storage.iter_objects()), None)
        next(iter(storage.incomplete_uploads()), None)
        storage.delete(key + "-multipart")
    except Exception:
        error = True
    finally:
        try:
            if storage and multipart_id:
                storage.abort(key + "-multipart", multipart_id)
            if storage:
                storage.delete(key)
                storage.delete(key + "-multipart")
        except Exception:
            error = True
    return not error


@router.post("/test")
def test_settings(payload: TestInput, db=Depends(get_db), actor=Depends(site_admin)):
    row = locked(db, payload.provider)
    if row.version != payload.version:
        raise HTTPException(409, "存储配置已更新，请重新加载后测试")
    error = not check_connection(row)
    row.test_result = "连接及文件操作验证通过" if not error else "连接测试失败，请检查凭据、桶地域及读写清理权限"
    row.tested_version = row.version if not error else None
    record(db, actor, "storage.tested", "storage", row.id, result="failure" if error else "success",
        details={"version": row.version, "accepted": not error})
    db.commit()
    return {**read(row), "success": not error}
