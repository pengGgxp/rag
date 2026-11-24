"""Administrator-owned connections, independent of models and workspace billing."""
import json
from time import monotonic
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .db import get_db
from .llm_providers import _encrypt_api_key, _decrypt_api_key, validate_base_url
from .model_pricing import settings_locked
from .models import ModelConnection, SiteModelProvider
from .site_auth import site_admin
from .site_audit import record

router = APIRouter(prefix="/site/admin/model-connections", tags=["site-admin"])
PLATFORMS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "adapter": "deepseek"},
    {"id": "bailian", "name": "阿里云百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "adapter": "bailian"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "adapter": "openai"},
    {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com/v1", "adapter": "none"},
    {"id": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "adapter": "none"},
    {"id": "custom", "name": "自定义兼容接口", "base_url": "", "adapter": "none"},
]


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    website_url: str = Field(default="", max_length=1000)
    platform: Literal["deepseek", "bailian", "openai", "anthropic", "gemini", "custom"] = "custom"
    api_protocol: Literal["chat_completions", "responses", "anthropic", "gemini"]
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr | None = Field(default=None, min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=600, strict=True)
    enabled: bool = True
    version: int = Field(default=0, ge=0, strict=True)


class ConnectionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True)


def connection_writer(request: Request, db=Depends(get_db), actor=Depends(site_admin)):
    try:
        yield actor
    except Exception:
        db.rollback()
        record(db, actor, "model_connections.management_failed", "model_connection",
            request.path_params.get("identifier", "new"), request=request, result="failure")
        db.commit()
        raise


def connection_for(db, row, *, refresh=False):
    return db.get(ModelConnection, row.connection_id, populate_existing=refresh)


def lock_connection(db, identifier, version=None):
    row = db.get(ModelConnection, identifier, with_for_update=True, populate_existing=True)
    if row is None:
        raise HTTPException(404, "供应商连接不存在")
    if version is not None and row.version != version:
        raise HTTPException(409, "供应商连接已更新，请重新加载后保存")
    return row


def read_connection(db, row):
    from .shared_providers import unavailable_reason
    models = db.scalars(select(SiteModelProvider).where(SiteModelProvider.connection_id == row.id)).all()
    return {"id": str(row.id), "name": row.name, "platform": row.platform,
        "api_protocol": row.api_protocol, "website_url": row.website_url,
        "base_url": row.base_url, "timeout_seconds": row.timeout_seconds,
        "has_api_key": bool(row.encrypted_api_key), "enabled": row.enabled, "version": row.version,
        "model_count": len(models), "available_model_count": sum(
            unavailable_reason(db, model, None, True) is None or bool(model.enabled and row.enabled and row.encrypted_api_key
                and (model.image_config.get("generate") or model.image_config.get("edit"))) for model in models)}


@router.get("/platforms")
def platforms(actor=Depends(site_admin)):
    return PLATFORMS


@router.get("/model-presets")
def model_presets(actor=Depends(site_admin)):
    # Exact official IDs only. Applying a preset is an explicit administrator action.
    return [{"platform": "deepseek", "model": model, "context_capacity": 1_000_000,
        "max_output_tokens": 384_000, "source": "https://api-docs.deepseek.com/quick_start/pricing",
        "verified_at": "2026-10-29", "thinking": {
            "kind": "effort", "adapter": "deepseek", "options": [
                {"id": "off", "label": "关闭", "value": "disabled"},
                {"id": "low", "label": "低", "value": "low"},
                {"id": "high", "label": "高", "value": "high"},
                {"id": "max", "label": "最高", "value": "max"}], "default_option_id": "high"}}
        for model in ("deepseek-flash", "deepseek-v4-pro")]


@router.get("/image-presets")
def image_presets(actor=Depends(site_admin)):
    from .images.presets import catalog
    return catalog()


@router.get("")
def list_connections(db=Depends(get_db), actor=Depends(site_admin)):
    return [read_connection(db, row) for row in db.scalars(select(ModelConnection).order_by(ModelConnection.created_at))]


@router.get("/{identifier}")
def get_connection(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(ModelConnection, identifier)
    if row is None:
        raise HTTPException(404, "供应商连接不存在")
    return read_connection(db, row)


def apply_connection(row, payload):
    if not payload.name.strip():
        raise HTTPException(422, "请填写供应商名称")
    row.name, row.platform = payload.name.strip(), payload.platform
    row.website_url = validate_base_url(payload.website_url) if payload.website_url else ""
    row.base_url = validate_base_url(payload.base_url)
    from .model_runtime.responses import endpoint
    if payload.api_protocol in ("chat_completions", "responses"):
        endpoint(row.base_url, payload.api_protocol)
    row.api_protocol = payload.api_protocol
    row.timeout_seconds, row.enabled = payload.timeout_seconds, payload.enabled
    if payload.api_key:
        row.encrypted_api_key = _encrypt_api_key(payload.api_key.get_secret_value())
    if row.enabled and not row.encrypted_api_key:
        raise HTTPException(422, "启用前请填写供应商密钥")


@router.post("", status_code=201)
def create_connection(payload: ConnectionInput, db=Depends(get_db), actor=Depends(connection_writer)):
    settings_locked(db)
    row = ModelConnection(encrypted_api_key="", version=1)
    apply_connection(row, payload)
    db.add(row)
    db.flush()
    record(db, actor, "model_connections.created", "model_connection", row.id,
           details={"fields": sorted(payload.model_fields_set - {"api_key"})})
    db.commit()
    return read_connection(db, row)


@router.put("/{identifier}")
def update_connection(identifier: UUID, payload: ConnectionInput, db=Depends(get_db), actor=Depends(connection_writer)):
    settings_locked(db)
    row = lock_connection(db, identifier, payload.version)
    from .model_runtime.responses import validate_thinking
    from .thinking import select_thinking
    for model in db.scalars(select(SiteModelProvider).where(SiteModelProvider.connection_id == identifier)):
        try:
            validate_thinking(model.chat_protocol, select_thinking(model.thinking_config, model.thinking_version))
        except ValueError as exc:
            raise HTTPException(422, f"{model.name}：{exc}") from None
    apply_connection(row, payload)
    row.version += 1
    record(db, actor, "model_connections.updated", "model_connection", row.id,
           details={"fields": sorted(payload.model_fields_set - {"api_key"})})
    db.commit()
    return read_connection(db, row)


@router.delete("/{identifier}", status_code=204)
def delete_connection(identifier: UUID, payload: ConnectionVersion, db=Depends(get_db), actor=Depends(connection_writer)):
    settings_locked(db)
    row = lock_connection(db, identifier, payload.version)
    if db.scalar(select(SiteModelProvider.id).where(SiteModelProvider.connection_id == identifier).limit(1)):
        raise HTTPException(409, "供应商仍有模型，请停用或先移除模型")
    record(db, actor, "model_connections.deleted", "model_connection", row.id)
    db.delete(row)
    db.commit()


def discover_models(url, secret, timeout, protocol="chat_completions"):
    # Bounded read; credentials stay on the server and are never forwarded on redirects.
    try:
        deadline = monotonic() + min(timeout, 10)
        from .model_runtime.responses import api_root
        root = api_root(url)
        if protocol == "gemini" and not root.endswith(("/v1", "/v1beta")):
            root += "/v1beta"
        headers = {"x-goog-api-key": secret} if protocol == "gemini" else {"x-api-key": secret, "anthropic-version": "2023-06-01"} if protocol == "anthropic" else {"Authorization": f"Bearer {secret}"}
        with httpx.stream("GET", f"{root}/models",
                headers=headers, timeout=min(timeout, 10),
                follow_redirects=False) as response:
            if response.status_code != 200:
                raise HTTPException(502, "未能获取模型列表，请检查连接或手动添加模型")
            raw = bytearray()
            for chunk in response.iter_bytes():
                if monotonic() > deadline:
                    raise HTTPException(502, "获取模型列表超时，请重试或手动添加模型")
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise HTTPException(502, "模型列表过大，请手动添加模型")
        data = json.loads(raw)
        entries = data.get("models" if protocol == "gemini" else "data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("invalid model list")
        field = "name" if protocol == "gemini" else "id"
        return sorted({item[field].strip().removeprefix("models/") for item in entries if isinstance(item, dict)
            and isinstance(item.get(field), str) and 0 < len(item[field].strip()) <= 200})[:1000]
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "未能获取模型列表，请检查连接或手动添加模型") from None


@router.post("/{identifier}/models/discover")
def discover(identifier: UUID, payload: ConnectionVersion, db=Depends(get_db), actor=Depends(site_admin)):
    row = lock_connection(db, identifier, payload.version)
    try:
        secret = _decrypt_api_key(row.encrypted_api_key)
    except Exception:
        raise HTTPException(503, "连接密钥暂不可用，请重新保存密钥") from None
    if not secret:
        raise HTTPException(422, "请先保存供应商密钥")
    url, timeout, version = row.base_url, row.timeout_seconds, row.version
    db.commit()
    try:
        models = discover_models(url, secret, timeout, row.api_protocol) if row.api_protocol in ("anthropic", "gemini") else discover_models(url, secret, timeout)
    except HTTPException:
        record(db, actor, "model_connections.discovered", "model_connection", identifier, result="failure")
        db.commit()
        raise
    lock_connection(db, identifier, version)
    record(db, actor, "model_connections.discovered", "model_connection", identifier, details={"count": len(models)})
    db.commit()
    return {"models": models, "connection_version": version}
