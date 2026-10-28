"""Workspace associations and compatible administrator price endpoints."""
from urllib.parse import urlparse
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import object_session
from . import mcp_client
from .management_access import ManagementAccess, management_access
from .credits import ensure_policies
from .db import get_db
from .llm import ChatProviderConfig
from .models import LLMProviderConfig, UsagePolicy, SiteModelProvider
from .model_pricing import prices_read, serialize_policy

router = APIRouter(prefix="/settings/llm-providers", tags=["llm-providers"])
site_router = APIRouter(prefix="/site/admin/workspaces/{workspace_id}/llm-providers", tags=["site-admin"])


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    enabled: bool = False


class ProviderPricingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(pattern=r"^llm_(input|cached_input|output)_tokens$")
    unit: str = Field(pattern=r"^tokens$")
    unit_size: int = Field(ge=1, le=2_147_483_647, strict=True)
    credits_per_unit: int = Field(ge=0, le=2_147_483_647, strict=True)
    enabled: bool = True
    version: int = Field(default=0, ge=0)


def validate_base_url(value):
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "供应商地址必须是不含凭据、查询参数的 HTTP 或 HTTPS URL")
    return value.strip().rstrip("/")


def provider_read(row):
    db = object_session(row)
    shared = db.get(SiteModelProvider, row.site_provider_id) if db and row.site_provider_id else None
    return {"id": str(row.id), "key": row.key, "name": shared.name if shared else row.name,
        "model": shared.model if shared else row.model, "enabled": bool(shared and shared.enabled),
        "active": row.active, "site_provider_id": str(row.site_provider_id) if row.site_provider_id else None}


def _encrypt_api_key(value):
    try:
        return mcp_client.encrypt_headers({"api_key": value})
    except mcp_client.MCPError as exc:
        raise HTTPException(503, "尚未配置服务端凭据加密密钥") from exc


def _decrypt_api_key(value):
    return mcp_client.decrypt_headers(value).get("api_key", "")


def request_provider_config(db, workspace_id, site_provider_id, thinking_option_id=None, thinking_config_version=None):
    from .shared_providers import request_binding
    from .thinking import select_thinking
    row, shared = request_binding(db, workspace_id, site_provider_id)
    thinking = select_thinking(shared.thinking_config, shared.thinking_version,
                              thinking_option_id, thinking_config_version)
    try:
        api_key = _decrypt_api_key(shared.encrypted_api_key)
    except Exception as exc:
        raise HTTPException(503, "模型服务尚未就绪，请联系站点管理员") from exc
    if not api_key:
        raise HTTPException(503, "模型服务尚未就绪，请联系站点管理员")
    return ChatProviderConfig(base_url=shared.base_url, api_key=api_key, model=shared.model,
        timeout_seconds=shared.timeout_seconds, provider_id=str(row.id), provider_key=row.key,
        provider_name=shared.name, site_provider_id=str(shared.id), thinking=thinking)


@site_router.get("")
@router.get("")
def list_providers(db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    return [provider_read(row) for row in db.scalars(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id).order_by(LLMProviderConfig.created_at))]


@site_router.get("/current")
@router.get("/current")
def current_provider(db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.active.is_(True)))
    return provider_read(row) if row else None


@site_router.post("")
@router.post("")
@site_router.put("/{identifier}")
@router.put("/{identifier}")
@site_router.post("/{identifier}/activate")
@router.post("/{identifier}/activate")
@site_router.delete("/{identifier}")
@router.delete("/{identifier}")
def retired_connection_write(access: ManagementAccess = Depends(management_access)):
    raise HTTPException(410, "供应商连接在站点后台配置；使用模型请在问答输入区选择")


def scoped_provider(db, access, identifier):
    row = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id))
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    return row


@site_router.get("/{identifier}/pricing")
@router.get("/{identifier}/pricing")
def list_provider_pricing(identifier: UUID, db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    scoped_provider(db, access, identifier)
    return prices_read(ensure_policies(db, access.workspace_id, identifier))


@site_router.put("/{identifier}/pricing/{capability}")
@router.put("/{identifier}/pricing/{capability}")
def update_provider_pricing(identifier: UUID, capability: str, payload: ProviderPricingInput,
                            db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    scoped_provider(db, access, identifier)
    raise HTTPException(410, "工作区独立价格已停止编辑，请在站点后台维护共享基准价与工作区倍率")
