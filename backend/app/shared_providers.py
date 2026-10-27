"""Site connections and workspace selections, with shared base prices."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update

from .auth import Access, get_access
from .db import get_db
from .llm_providers import ProviderInput, _encrypt_api_key, _decrypt_api_key, validate_base_url
from .management_access import ManagementAccess, management_access
from .model_billing import CAPABILITIES
from .model_pricing import ModelPrice, complete, settings_locked
from .models import LLMProviderConfig, ModelBillingSettings, SiteModelProvider, UsagePolicy, Workspace
from .site_auth import site_admin
from .site_audit import record

router = APIRouter(prefix="/site/admin/llm-providers", tags=["site-admin"])
selection_router = APIRouter(tags=["models"])


def provider_writer(request: Request, db=Depends(get_db), actor=Depends(site_admin)):
    try:
        yield actor
    except Exception:
        db.rollback()
        record(db, actor, "models.management_failed", "model_provider",
            request.path_params.get("identifier", "new"), request=request, result="failure")
        db.commit()
        raise


class SiteProviderInput(ProviderInput):
    enabled: bool = False
    published: bool = True
    version: int = Field(default=0, ge=0)


class PriceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    policies: list[ModelPrice] = Field(max_length=3)


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: UUID
    version: int = Field(ge=0)


def lock_provider(db, identifier, version=None):
    row = db.get(SiteModelProvider, identifier, with_for_update=True, populate_existing=True)
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    if version is not None and row.version != version:
        raise HTTPException(409, "供应商已更新，请重新加载后保存")
    return row


def read_provider(db, row):
    settings = db.get(ModelBillingSettings, 1)
    return {"id": str(row.id), "key": row.key, "name": row.name, "model": row.model,
        "base_url": row.base_url, "timeout_seconds": row.timeout_seconds,
        "has_api_key": bool(row.encrypted_api_key), "enabled": row.enabled,
        "published": row.published, "version": row.version,
        "is_default": bool(settings and settings.default_provider_id == row.id),
        "configured": complete(row.prices), "prices": list(row.prices.values()),
        "workspace_count": db.scalar(select(func.count()).select_from(LLMProviderConfig).where(
            LLMProviderConfig.site_provider_id == row.id)),
        "updated_at": row.updated_at.isoformat()}


def apply_provider(row, payload):
    row.key, row.name = payload.key, payload.name.strip()
    row.base_url, row.model = validate_base_url(payload.base_url), payload.model.strip()
    if not row.name or not row.model:
        raise HTTPException(422, "名称和模型不能为空")
    row.timeout_seconds, row.published = payload.timeout_seconds, payload.published
    if payload.api_key:
        row.encrypted_api_key = _encrypt_api_key(payload.api_key.get_secret_value())
    if not row.encrypted_api_key:
        raise HTTPException(422, "请配置供应商密钥")
    if payload.enabled and not row.enabled and not complete(row.prices):
        raise HTTPException(422, "启用前请补齐三项模型价格")
    row.enabled = payload.enabled


@router.get("")
def list_global(db=Depends(get_db), actor=Depends(site_admin)):
    return [read_provider(db, row) for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.created_at))]


@router.post("", status_code=201)
def create_global(payload: SiteProviderInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings = settings_locked(db)
    if db.scalar(select(SiteModelProvider.id).where(SiteModelProvider.key == payload.key)):
        raise HTTPException(409, "供应商标识已存在")
    row = SiteModelProvider(encrypted_api_key="", enabled=False, prices={key: dict(value)
        for key, value in settings.template.items() if key in CAPABILITIES}, template_version=settings.version)
    apply_provider(row, payload)
    db.add(row)
    db.flush()
    record(db, actor, "models.created", "model_provider", row.id, details={"fields": sorted(payload.model_fields_set - {"api_key"})})
    db.commit()
    return read_provider(db, row)


@router.put("/{identifier}")
def update_global(identifier: UUID, payload: SiteProviderInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings_locked(db)
    row = lock_provider(db, identifier, payload.version)
    if db.scalar(select(SiteModelProvider.id).where(SiteModelProvider.key == payload.key, SiteModelProvider.id != identifier)):
        raise HTTPException(409, "供应商标识已存在")
    apply_provider(row, payload)
    row.version += 1
    record(db, actor, "models.updated", "model_provider", row.id, details={"fields": sorted(payload.model_fields_set - {"api_key"})})
    db.commit()
    return read_provider(db, row)


@router.put("/{identifier}/pricing")
def update_prices(identifier: UUID, payload: PriceUpdate, db=Depends(get_db), actor=Depends(provider_writer)):
    settings_locked(db)
    row = lock_provider(db, identifier, payload.version)
    prices = {p.capability: p.model_dump() for p in payload.policies}
    if len(prices) != len(payload.policies) or not complete(prices):
        raise HTTPException(422, "请完整填写三项模型价格，免费请明确填写 0")
    row.prices, row.version = prices, row.version + 1
    record(db, actor, "models.base_prices_updated", "model_provider", row.id, details={"fields": list(prices)})
    db.commit()
    return read_provider(db, row)


@router.post("/{identifier}/default")
def set_default(identifier: UUID, payload: VersionInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings = settings_locked(db)
    row = lock_provider(db, identifier, payload.version)
    if not row.enabled or not row.published or not complete(row.prices):
        raise HTTPException(422, "默认模型必须已发布、启用且价格完整")
    settings.default_provider_id = row.id
    row.version += 1
    record(db, actor, "models.default_selected", "model_provider", row.id)
    db.commit()
    return read_provider(db, row)


@router.delete("/{identifier}", status_code=204)
def delete_global(identifier: UUID, payload: VersionInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings = settings_locked(db)
    row = lock_provider(db, identifier, payload.version)
    if settings.default_provider_id == row.id or db.scalar(select(LLMProviderConfig.id).where(LLMProviderConfig.site_provider_id == row.id).limit(1)):
        raise HTTPException(409, "供应商已被引用，请停用而非删除")
    record(db, actor, "models.deleted", "model_provider", row.id)
    db.delete(row)
    db.commit()


def unavailable_reason(db, provider, binding, shared):
    if not provider.enabled:
        return "model_disabled"
    if not provider.encrypted_api_key or not provider.model or not provider.base_url:
        return "service_not_ready"
    if shared:
        prices = provider.prices
    elif binding is None:
        return "service_not_ready"
    else:
        prices = {p.capability: {key: getattr(p, key) for key in (
            "capability", "unit", "unit_size", "credits_per_unit", "enabled")}
            for p in db.scalars(select(UsagePolicy).where(
                UsagePolicy.provider_id == binding.id, UsagePolicy.capability.in_(CAPABILITIES)))}
    return None if complete(prices) else "pricing_unconfigured"


def choices(db, workspace_id):
    from .workspace_billing import shared_enabled
    shared = shared_enabled(db)
    workspace = db.get(Workspace, workspace_id)
    bindings = db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id)).all()
    ids = [row.site_provider_id for row in bindings if row.site_provider_id]
    rows = db.scalars(select(SiteModelProvider).where(or_(SiteModelProvider.published.is_(True), SiteModelProvider.id.in_(ids))).order_by(SiteModelProvider.created_at)).all()
    active = next((row for row in bindings if row.active), None)
    by_provider = {binding.site_provider_id: binding for binding in bindings}
    providers = []
    for row in rows:
        reason = unavailable_reason(db, row, by_provider.get(row.id), shared)
        providers.append({"id": str(row.id), "name": row.name, "model": row.model,
            "available": reason is None, "unavailable_reason": reason, "enabled": row.enabled})
    return {"version": workspace.model_version, "provider_id": str(active.site_provider_id) if active and active.site_provider_id else None,
        "providers": providers}


def select_model(db, workspace_id, identifier):
    # Workspace lock precedes provider lock for all selection/default initialization paths.
    db.get(Workspace, workspace_id, with_for_update=True, populate_existing=True)
    row = lock_provider(db, identifier)
    binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id,
        LLMProviderConfig.site_provider_id == identifier))
    from .workspace_billing import shared_enabled
    shared = shared_enabled(db)
    if not row.published and binding is None:
        raise HTTPException(404, "模型不存在或不可用")
    reason = unavailable_reason(db, row, binding, shared)
    if reason:
        messages = {"model_disabled": "模型已停用，请选择其他模型",
            "pricing_unconfigured": "模型价格尚未配置完整，请联系站点管理员",
            "service_not_ready": "模型服务尚未就绪，请联系站点管理员"}
        raise HTTPException(409, messages[reason])
    if binding is None:
        binding = LLMProviderConfig(workspace_id=workspace_id, site_provider_id=row.id,
            key=f"shared-{row.id}", name=row.name, base_url="", model=row.model,
            encrypted_api_key="", enabled=True, active=False, source_price_version=row.version)
        db.add(binding)
        db.flush()
    db.execute(update(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id).values(active=False))
    binding.active = True
    db.get(Workspace, workspace_id).model_version += 1
    db.flush()


@selection_router.get("/settings/models")
def workspace_models(db=Depends(get_db), access: Access = Depends(get_access)):
    return choices(db, access.workspace_id)


@selection_router.put("/settings/models")
def select_workspace_model(payload: SelectionInput, db=Depends(get_db), access: Access = Depends(get_access)):
    access.admin()
    workspace = db.get(Workspace, access.workspace_id, with_for_update=True, populate_existing=True)
    if payload.version != workspace.model_version:
        raise HTTPException(409, "工作区模型已更新，请重新加载")
    select_model(db, workspace.id, payload.provider_id)
    from .audit_log import record_audit
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="workspace.model_selected", resource_type="model_provider", resource_id=payload.provider_id)
    db.commit()
    return choices(db, workspace.id)


@selection_router.get("/site/admin/workspaces/{workspace_id}/model")
def admin_workspace_model(db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    return choices(db, access.workspace_id)


@selection_router.put("/site/admin/workspaces/{workspace_id}/model")
def admin_select_model(payload: SelectionInput, db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    if payload.version != db.get(Workspace, access.workspace_id).model_version:
        raise HTTPException(409, "工作区模型已更新，请重新加载")
    select_model(db, access.workspace_id, payload.provider_id)
    access.audit(db, "workspace.model_selected", target_id=payload.provider_id)
    db.commit()
    return choices(db, access.workspace_id)


def initialize_default(db, workspace_id):
    settings = db.get(ModelBillingSettings, 1)
    row = db.get(SiteModelProvider, settings.default_provider_id) if settings and settings.default_provider_id else None
    from .workspace_billing import shared_enabled
    if shared_enabled(db) and row and row.published and row.enabled and complete(row.prices):
        select_model(db, workspace_id, row.id)


def assert_available(identifier):
    from .db import SessionLocal
    from .llm import LLMConfigurationError
    with SessionLocal() as db:
        row = db.get(SiteModelProvider, UUID(identifier))
        if row is None or not row.enabled:
            raise LLMConfigurationError("当前模型已停用，请选择其他模型")


def migrate_connections(db, settings):
    """Console only: preserve old associations/prices and never print credentials."""
    from .models import Conversation, ModelUsageRequest
    if db.scalar(select(Conversation.id).where(Conversation.active_request_id.is_not(None)).limit(1)) or db.scalar(
        select(ModelUsageRequest.request_id).where(ModelUsageRequest.status == "running").limit(1)):
        raise ValueError("Drain active model requests before migrating connections.")
    migration = settings_locked(db)
    if migration.connections_migrated:
        return 0
    count = 0
    for binding in db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.site_provider_id.is_(None))):
        _decrypt_api_key(binding.encrypted_api_key)
        prices = {p.capability: {key: getattr(p, key) for key in ("capability", "unit", "unit_size", "credits_per_unit", "enabled")}
            for p in db.scalars(select(UsagePolicy).where(UsagePolicy.provider_id == binding.id, UsagePolicy.capability.in_(CAPABILITIES)))}
        row = SiteModelProvider(key=f"legacy-{binding.id}", name=binding.name, base_url=binding.base_url,
            model=binding.model, encrypted_api_key=binding.encrypted_api_key, timeout_seconds=binding.timeout_seconds,
            enabled=binding.enabled, published=False, prices=prices)
        db.add(row)
        db.flush()
        binding.site_provider_id, binding.encrypted_api_key, binding.base_url = row.id, "", ""
        count += 1
    targets = [w for w in db.scalars(select(Workspace)) if not db.scalar(select(LLMProviderConfig.id).where(
        LLMProviderConfig.workspace_id == w.id, LLMProviderConfig.active.is_(True)))]
    if targets:
        if not settings.llm_configured:
            raise ValueError("Legacy environment model is incomplete; migration aborted.")
        row = db.scalar(select(SiteModelProvider).where(SiteModelProvider.key == "legacy-environment"))
        if row is None:
            row = SiteModelProvider(key="legacy-environment", name="平台模型", base_url=validate_base_url(settings.llm_base_url),
                model=settings.llm_model, encrypted_api_key=_encrypt_api_key(settings.llm_api_key.get_secret_value()),
                timeout_seconds=settings.llm_timeout_seconds, enabled=True, published=True, prices={})
            db.add(row)
            db.flush()
        for workspace in targets:
            binding = LLMProviderConfig(workspace_id=workspace.id, site_provider_id=row.id, key="legacy-environment",
                name=row.name, model=row.model, base_url="", encrypted_api_key="", active=True, enabled=True)
            db.add(binding)
            db.flush()
            for policy in db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace.id,
                UsagePolicy.provider_id.is_(None), UsagePolicy.capability.in_(CAPABILITIES))):
                policy.provider_id = binding.id
            count += 1
    db.flush()
    for binding in db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.active.is_(True))):
        row = db.get(SiteModelProvider, binding.site_provider_id)
        if not row or not row.enabled or not _decrypt_api_key(row.encrypted_api_key):
            raise ValueError("An active workspace connection is unavailable; migration aborted.")
    migration.connections_migrated = True
    record(db, None, "models.migrated", "model_provider", "migration", details={"associations": count})
    return count
