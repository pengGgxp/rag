"""Site connections and request-scoped accounting associations with shared prices."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from .auth import Access, get_access
from .db import get_db
from .llm_providers import _encrypt_api_key, _decrypt_api_key, validate_base_url
from .model_connections import connection_for, lock_connection, router as connection_router
from .model_billing import CAPABILITIES
from .model_pricing import ModelPrice, complete, settings_locked
from .models import (
    LLMProviderConfig,
    ModelBillingSettings,
    ModelConnection,
    SiteModelProvider,
    UsagePolicy,
    UserModelPolicy,
    User,
)
from .site_auth import site_admin
from .site_audit import record
from .thinking import ThinkingConfig, public_capability
from .model_capabilities import ChatProtocol, ImageConfig

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


class SiteProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    connection_id: UUID
    connection_version: int = Field(ge=1, strict=True)
    policies: list[ModelPrice] | None = Field(default=None, max_length=3)
    enabled: bool = False
    supports_images: bool = Field(default=False, strict=True)
    supports_chat: bool = True
    chat_protocol: ChatProtocol | None = None
    image_config: ImageConfig = Field(default_factory=ImageConfig)
    version: int = Field(default=0, ge=0)
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    context_capacity: int | None = Field(default=None, ge=1024, le=100_000_000, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000, strict=True)

    @model_validator(mode="after")
    def valid_context(self):
        if (self.context_capacity is None) != (self.max_output_tokens is None):
            raise ValueError("请同时配置模型上下文容量和最大输出")
        if self.context_capacity and self.max_output_tokens >= self.context_capacity * 0.95:
            raise ValueError("最大输出必须小于上下文容量减去安全余量")
        return self


class PriceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    policies: list[ModelPrice] = Field(max_length=3)


class VersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class BatchModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_version: int = Field(ge=1, strict=True)
    models: list[SiteProviderInput] = Field(min_length=1, max_length=50)


def lock_provider(db, identifier, version=None):
    row = db.get(SiteModelProvider, identifier, with_for_update=True, populate_existing=True)
    if row is None:
        raise HTTPException(404, "模型供应商不存在")
    if version is not None and row.version != version:
        raise HTTPException(409, "供应商已更新，请重新加载后保存")
    return row


def read_provider(db, row):
    settings = db.get(ModelBillingSettings, 1)
    reason = unavailable_reason(db, row, None, True)
    connection = connection_for(db, row)
    return {"id": str(row.id), "key": row.key, "name": row.name, "model": row.model,
        "connection_id": str(row.connection_id), "connection_name": connection.name if connection else "",
        "connection_version": connection.version if connection else 0,
        "api_protocol": row.chat_protocol,
        "chat_protocol": row.chat_protocol, "supports_chat": row.supports_chat,
        "image_config": {**ImageConfig().model_dump(mode="json"), **(row.image_config or {})},
        "platform": connection.platform if connection else "custom", "enabled": row.enabled,
        "version": row.version,
        "supports_images": row.supports_images,
        "thinking": row.thinking_config or ThinkingConfig().model_dump(),
        "thinking_version": row.thinking_version,
        "context_capacity": row.context_capacity, "max_output_tokens": row.max_output_tokens,
        "is_default": bool(settings and settings.default_provider_id == row.id),
        "configured": complete(row.prices), "prices": list(row.prices.values()),
        "available": reason is None, "unavailable_reason": reason,
        "user_count": db.scalar(select(func.count()).select_from(LLMProviderConfig).where(
            LLMProviderConfig.site_provider_id == row.id)),
        "updated_at": row.updated_at.isoformat()}


def apply_provider(db, row, payload):
    connection = lock_connection(db, payload.connection_id, payload.connection_version)
    from .llm_responses import validate_thinking
    from .thinking import select_thinking
    try:
        validate_thinking(payload.chat_protocol or connection.api_protocol, select_thinking(payload.thinking.model_dump(), 0))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    row.connection_id, row.name, row.model = connection.id, payload.name.strip(), payload.model.strip()
    if not row.name or not row.model:
        raise HTTPException(422, "名称和模型不能为空")
    row.context_capacity, row.max_output_tokens = payload.context_capacity, payload.max_output_tokens
    row.supports_images = payload.supports_images
    row.chat_protocol = payload.chat_protocol or connection.api_protocol
    row.supports_chat = payload.supports_chat
    row.image_config = payload.image_config.model_dump(mode="json")
    if db.scalar(select(SiteModelProvider.id).where(SiteModelProvider.connection_id == connection.id,
            SiteModelProvider.model == row.model, SiteModelProvider.id != row.id)):
        raise HTTPException(422, f"该连接已配置模型 {row.model}")
    if payload.policies is not None:
        prices = {p.capability: p.model_dump() for p in payload.policies}
        if len(prices) != len(payload.policies):
            raise HTTPException(422, "模型价格项目重复")
        row.prices = prices
    if payload.enabled and not payload.supports_chat and not (payload.image_config.generate or payload.image_config.edit):
        raise HTTPException(422, "请至少配置一项模型能力")
    if payload.enabled and payload.supports_chat and not complete(row.prices):
        raise HTTPException(422, "启用前请补齐三项模型价格")
    if payload.enabled and (not connection.enabled or not connection.encrypted_api_key):
        raise HTTPException(422, "请先启用并完善供应商连接")
    row.enabled = payload.enabled
    thinking = payload.thinking.model_dump()
    if ThinkingConfig.model_validate(row.thinking_config or {}).model_dump() != thinking:
        row.thinking_config = thinking
        row.thinking_version = (row.thinking_version or 0) + 1


@router.get("")
def list_global(db=Depends(get_db), actor=Depends(site_admin)):
    return [read_provider(db, row) for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.created_at))]


@router.get("/{identifier}")
def read_global(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(SiteModelProvider, identifier)
    if row is None:
        raise HTTPException(404, "模型配置不存在")
    return read_provider(db, row)


@router.post("", status_code=201)
def create_global(payload: SiteProviderInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings = settings_locked(db)
    row = SiteModelProvider(key=f"model-{uuid4().hex}", enabled=False, prices={key: dict(value)
        for key, value in settings.template.items() if key in CAPABILITIES}, template_version=settings.version)
    apply_provider(db, row, payload)
    db.add(row)
    db.flush()
    record(db, actor, "models.created", "model_provider", row.id, details={"fields": sorted(payload.model_fields_set - {"api_key"})})
    db.commit()
    return read_provider(db, row)


@connection_router.post("/{identifier}/models", status_code=201)
def create_models(identifier: UUID, payload: BatchModels, db=Depends(get_db), actor=Depends(provider_writer)):
    settings = settings_locked(db)
    lock_connection(db, identifier, payload.connection_version)
    names = [item.model.strip() for item in payload.models]
    if len(set(names)) != len(names):
        raise HTTPException(422, "所选模型重复，请检查后保存")
    rows = []
    for item in payload.models:
        if item.connection_id != identifier or item.connection_version != payload.connection_version:
            raise HTTPException(422, "模型与供应商连接不匹配")
        row = SiteModelProvider(key=f"model-{uuid4().hex}", enabled=False, prices={
            key: dict(value) for key, value in settings.template.items() if key in CAPABILITIES},
            template_version=settings.version)
        try:
            apply_provider(db, row, item)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, f"{item.model}：{exc.detail}") from None
        db.add(row)
        db.flush()
        rows.append(row)
    record(db, actor, "models.batch_created", "model_connection", identifier, details={"count": len(rows)})
    db.commit()
    return [read_provider(db, row) for row in rows]


@router.put("/{identifier}")
def update_global(identifier: UUID, payload: SiteProviderInput, db=Depends(get_db), actor=Depends(provider_writer)):
    settings_locked(db)
    row = lock_provider(db, identifier, payload.version)
    apply_provider(db, row, payload)
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
    if unavailable_reason(db, row, None, True):
        raise HTTPException(422, "默认模型必须启用、连接就绪且价格完整")
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
    if not provider.supports_chat:
        return "not_chat_model"
    if not provider.enabled:
        return "model_disabled"
    connection = connection_for(db, provider, refresh=True)
    if connection is not None and not connection.enabled:
        return "connection_disabled"
    if not connection or not connection.encrypted_api_key or not provider.model or not connection.base_url:
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


def visible_providers(db, account_id):
    # Management previews retain disabled entries. User selection applies account policy.
    return db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.created_at)).all()


def choices(db, account_id):
    providers = []
    for row in visible_providers(db, account_id):
        reason = unavailable_reason(db, row, None, True)
        providers.append({"id": str(row.id), "name": row.name, "model": row.model,
            "supports_images": row.supports_images,
            "supports_tools": ThinkingConfig.model_validate(row.thinking_config or {}).supports_tools,
            "thinking": public_capability(row.thinking_config, row.thinking_version),
            "context_capacity": row.context_capacity, "max_output_tokens": row.max_output_tokens,
            "available": reason is None, "unavailable_reason": reason, "enabled": row.enabled})
    return providers


@selection_router.get("/models/available")
def available_models(db=Depends(get_db), access: Access = Depends(get_access)):
    policy = db.get(UserModelPolicy, access.user_id, populate_existing=True)
    allowed = {str(item) for item in policy.available_models} if policy is not None else None
    providers = [item for item in choices(db, access.user_id)
                 if allowed is None or item["id"] in allowed
                 if item["unavailable_reason"] not in ("model_disabled", "connection_disabled", "not_chat_model")]
    settings = db.get(ModelBillingSettings, 1)
    default = (
        policy.default_model
        if policy is not None
        else str(settings.default_provider_id) if settings and settings.default_provider_id else None
    )
    if not any(item["id"] == default and item["available"] for item in providers):
        default = None
    return {"default_provider_id": default, "providers": providers}


def request_binding(db, account_id, identifier):
    # Match administrator writes: settings before account/provider locks.
    db.scalar(select(ModelBillingSettings).where(ModelBillingSettings.id == 1).with_for_update(read=True))
    db.get(User, account_id, with_for_update=True, populate_existing=True)
    row = lock_provider(db, identifier)
    policy = db.get(UserModelPolicy, account_id, populate_existing=True)
    if policy is not None and str(row.id) not in {str(item) for item in policy.available_models or []}:
        raise HTTPException(403, "当前账号未开通此模型")
    binding = db.scalar(select(LLMProviderConfig).where(
        LLMProviderConfig.account_id == account_id,
        LLMProviderConfig.site_provider_id == identifier))
    reason = unavailable_reason(db, row, binding, True)
    if reason:
        messages = {"not_chat_model": "此模型不支持对话，请选择其他模型",
            "model_disabled": "模型已停用，请选择其他模型",
            "connection_disabled": "模型服务已停用，请选择其他模型",
            "pricing_unconfigured": "模型价格尚未配置完整，请联系站点管理员",
            "service_not_ready": "模型服务尚未就绪，请联系站点管理员"}
        raise HTTPException(409, messages[reason])
    if binding is None:
        binding = LLMProviderConfig(account_id=account_id, site_provider_id=row.id,
            key=f"shared-{row.id}", name=row.name, base_url="", model=row.model,
            encrypted_api_key="", enabled=True, active=False)
        db.add(binding)
        db.flush()
    return binding, row


def assert_available(identifier, connection_id=None):
    from .db import SessionLocal
    from .llm import LLMConfigurationError
    with SessionLocal() as db:
        row = db.get(SiteModelProvider, UUID(identifier))
        connection = (db.get(ModelConnection, UUID(connection_id)) if connection_id
            else connection_for(db, row) if row else None)
        if row is None or not row.enabled or not connection or not connection.enabled:
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
    for binding in db.scalars(select(LLMProviderConfig).where(
            LLMProviderConfig.site_provider_id.is_(None), LLMProviderConfig.encrypted_api_key != "")):
        _decrypt_api_key(binding.encrypted_api_key)
        prices = {p.capability: {key: getattr(p, key) for key in ("capability", "unit", "unit_size", "credits_per_unit", "enabled")}
            for p in db.scalars(select(UsagePolicy).where(UsagePolicy.provider_id == binding.id, UsagePolicy.capability.in_(CAPABILITIES)))}
        prices = {key: ModelPrice.model_validate(value).model_dump() for key, value in prices.items()}
        connection = ModelConnection(name=binding.name, platform="custom", base_url=binding.base_url,
            encrypted_api_key=binding.encrypted_api_key, timeout_seconds=binding.timeout_seconds, enabled=True)
        db.add(connection)
        db.flush()
        row = SiteModelProvider(key=f"legacy-{binding.id}", name=binding.name, connection_id=connection.id,
            model=binding.model,
            enabled=binding.enabled, published=False, prices=prices)
        db.add(row)
        db.flush()
        binding.site_provider_id, binding.encrypted_api_key, binding.base_url = row.id, "", ""
        count += 1
    targets = [w for w in db.scalars(select(User).where(User.is_suspended.is_(False))) if not db.scalar(select(LLMProviderConfig.id).where(
        LLMProviderConfig.account_id == w.id, LLMProviderConfig.active.is_(True)))]
    if targets:
        if not settings.llm_configured:
            raise ValueError("Legacy environment model is incomplete; migration aborted.")
        row = db.scalar(select(SiteModelProvider).where(SiteModelProvider.key == "legacy-environment"))
        if row is None:
            connection = ModelConnection(name="平台模型", platform="custom", base_url=validate_base_url(settings.llm_base_url),
                encrypted_api_key=_encrypt_api_key(settings.llm_api_key.get_secret_value()),
                timeout_seconds=settings.llm_timeout_seconds, enabled=True)
            db.add(connection)
            db.flush()
            row = SiteModelProvider(key="legacy-environment", name="平台模型", connection_id=connection.id,
                model=settings.llm_model, enabled=True, published=True, prices={})
            db.add(row)
            db.flush()
        for workspace in targets:
            binding = LLMProviderConfig(account_id=workspace.id, site_provider_id=row.id, key="legacy-environment",
                name=row.name, model=row.model, base_url="", encrypted_api_key="", active=True, enabled=True)
            db.add(binding)
            db.flush()
            for policy in db.scalars(select(UsagePolicy).where(UsagePolicy.account_id == workspace.id,
                UsagePolicy.provider_id.is_(None), UsagePolicy.capability.in_(CAPABILITIES))):
                policy.provider_id = binding.id
            count += 1
    db.flush()
    for binding in db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.active.is_(True))):
        row = db.get(SiteModelProvider, binding.site_provider_id)
        connection = connection_for(db, row) if row else None
        if not row or not row.enabled or not connection or not _decrypt_api_key(connection.encrypted_api_key):
            raise ValueError("An active workspace connection is unavailable; migration aborted.")
    migration.connections_migrated = True
    record(db, None, "models.migrated", "model_provider", "migration", details={"associations": count})
    return count
