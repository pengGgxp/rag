"""Admin-managed OpenAI-compatible chat model providers."""

from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import mcp_client
from .management_access import ManagementAccess, management_access
from .credits import ensure_policies
from .db import get_db
from .llm import ChatProviderConfig, LLMConfigurationError, OpenAICompatibleChatProvider
from .models import LLMProviderConfig, UsagePolicy

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
    enabled: bool = True


class ProviderPricingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(pattern=r"^llm_(input|output)_tokens$")
    unit: str = Field(min_length=1, max_length=40)
    unit_size: int = Field(default=1000, ge=1)
    credits_per_unit: int = Field(default=0, ge=0)
    enabled: bool = True


def validate_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, "模型供应商地址必须是 HTTP 或 HTTPS URL")
    return value.strip().rstrip("/")


def provider_read(row: LLMProviderConfig) -> dict:
    return {
        "id": str(row.id),
        "key": row.key,
        "name": row.name,
        "base_url": row.base_url,
        "model": row.model,
        "timeout_seconds": row.timeout_seconds,
        "enabled": row.enabled,
        "active": row.active,
        "has_api_key": bool(row.encrypted_api_key),
        "updated_at": row.updated_at.isoformat(),
    }


def _encrypt_api_key(value: str) -> str:
    try:
        return mcp_client.encrypt_headers({"api_key": value})
    except mcp_client.MCPError as exc:
        raise HTTPException(503, "尚未配置服务端凭据加密密钥，请先配置 MCP_CREDENTIAL_KEY") from exc


def _decrypt_api_key(value: str) -> str:
    headers = mcp_client.decrypt_headers(value)
    return headers.get("api_key", "")


def active_provider_config(db: Session, workspace_id: UUID) -> ChatProviderConfig | None:
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == workspace_id,
        LLMProviderConfig.active.is_(True),
        LLMProviderConfig.enabled.is_(True),
    ))
    if row is None:
        return None
    try:
        api_key = _decrypt_api_key(row.encrypted_api_key)
    except Exception as exc:
        raise LLMConfigurationError("当前模型供应商密钥无法解密，请重新配置") from exc
    return ChatProviderConfig(
        base_url=row.base_url,
        api_key=api_key,
        model=row.model,
        timeout_seconds=row.timeout_seconds,
        provider_id=str(row.id),
        provider_key=row.key,
        provider_name=row.name,
    )


def provider_for_workspace(db: Session, workspace_id: UUID) -> OpenAICompatibleChatProvider:
    config = active_provider_config(db, workspace_id)
    return OpenAICompatibleChatProvider(config=config) if config else OpenAICompatibleChatProvider()


def _apply(row: LLMProviderConfig, payload: ProviderInput, *, preserve_key: bool = True):
    row.key = payload.key
    row.name = payload.name.strip()
    row.base_url = validate_base_url(payload.base_url)
    row.model = payload.model.strip()
    row.timeout_seconds = payload.timeout_seconds
    row.enabled = payload.enabled
    if payload.api_key is not None:
        row.encrypted_api_key = _encrypt_api_key(payload.api_key.get_secret_value())
    elif not preserve_key or not row.encrypted_api_key:
        raise HTTPException(422, "请提供模型供应商 API Key")


@site_router.get("")
@router.get("")
def list_providers(db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    return [provider_read(row) for row in db.scalars(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
    ).order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)).all()]


@site_router.get("/current")
@router.get("/current")
def current_provider(db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.active.is_(True),
    ))
    return provider_read(row) if row is not None else None


@site_router.post("", status_code=201)
@router.post("", status_code=201)
def create_provider(payload: ProviderInput, db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    if payload.api_key is None:
        raise HTTPException(422, "请提供模型供应商 API Key")
    existing = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.key == payload.key,
    ))
    if existing is not None:
        raise HTTPException(409, "供应商标识已存在")
    row = LLMProviderConfig(workspace_id=access.workspace_id, encrypted_api_key="")
    _apply(row, payload, preserve_key=False)
    if not db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.active.is_(True),
    )):
        row.active = True
    db.add(row)
    db.flush()
    access.audit(db, "workspace.model_provider_created", target_id=row.id, fields=payload.model_fields_set)
    db.commit()
    db.refresh(row)
    return provider_read(row)


@site_router.put("/{identifier}")
@router.put("/{identifier}")
def update_provider(identifier: UUID, payload: ProviderInput,
                    db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    duplicate = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.key == payload.key,
        LLMProviderConfig.id != row.id,
    ))
    if duplicate is not None:
        raise HTTPException(409, "供应商标识已存在")
    _apply(row, payload)
    if row.active and not row.enabled:
        row.active = False
    access.audit(db, "workspace.model_provider_updated", target_id=row.id, fields=payload.model_fields_set)
    db.commit()
    db.refresh(row)
    return provider_read(row)


@site_router.post("/{identifier}/activate")
@router.post("/{identifier}/activate")
def activate_provider(identifier: UUID, db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id,
        LLMProviderConfig.enabled.is_(True),
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "启用的模型供应商不存在")
    db.execute(update(LLMProviderConfig).where(
        LLMProviderConfig.workspace_id == access.workspace_id,
    ).values(active=False))
    row.active = True
    access.audit(db, "workspace.model_provider_activated", target_id=row.id, fields=("active",))
    db.commit()
    db.refresh(row)
    return provider_read(row)


@site_router.delete("/{identifier}", status_code=204)
@router.delete("/{identifier}", status_code=204)
def delete_provider(identifier: UUID, db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    if row.active:
        raise HTTPException(409, "当前供应商正在使用，请先切换其他供应商")
    db.delete(row)
    access.audit(db, "workspace.model_provider_deleted", target_id=row.id)
    db.commit()


@site_router.get("/{identifier}/pricing")
@router.get("/{identifier}/pricing")
def list_provider_pricing(identifier: UUID, db: Session = Depends(get_db),
                          access: ManagementAccess = Depends(management_access)):
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id,
    ))
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    policies = ensure_policies(db, access.workspace_id, row.id)
    db.commit()
    return [{
        "capability": item.capability, "unit": item.unit,
        "unit_size": item.unit_size, "credits_per_unit": item.credits_per_unit,
        "enabled": item.enabled, "version": item.version,
    } for item in policies if item.capability.startswith("llm_")]


@site_router.put("/{identifier}/pricing/{capability}")
@router.put("/{identifier}/pricing/{capability}")
def update_provider_pricing(identifier: UUID, capability: str, payload: ProviderPricingInput,
                            db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
    if capability != payload.capability:
        raise HTTPException(422, "能力标识不一致")
    row = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.id == identifier,
        LLMProviderConfig.workspace_id == access.workspace_id,
    ))
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    policies = ensure_policies(db, access.workspace_id, row.id)
    policy = next((item for item in policies if item.capability == capability), None)
    if policy is None:
        policy = UsagePolicy(
            workspace_id=access.workspace_id, provider_id=row.id,
            capability=capability, unit=payload.unit, unit_size=payload.unit_size,
            credits_per_unit=payload.credits_per_unit, enabled=payload.enabled,
        )
        db.add(policy)
    else:
        policy.version += 1
        policy.unit = payload.unit
        policy.unit_size = payload.unit_size
        policy.credits_per_unit = payload.credits_per_unit
        policy.enabled = payload.enabled
    policy.updated_by = access.user_id
    access.audit(db, "workspace.model_pricing_updated", target_id=row.id, fields=payload.model_fields_set)
    db.commit()
    return {
        "capability": policy.capability, "unit": policy.unit,
        "unit_size": policy.unit_size, "credits_per_unit": policy.credits_per_unit,
        "enabled": policy.enabled, "version": policy.version,
    }
