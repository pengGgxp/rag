"""Safe parameter previews and metered, idempotent thinking probes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from . import model_billing
from .auth import Access, get_access
from .credits import authorization_hold, authorization_hold_amount, settle_model_usage
from .db import get_db
from .model_runtime.client import ChatProviderConfig, LLMConfigurationError, LLMProviderError, OpenAICompatibleChatProvider
from .llm_providers import request_provider_config
from .models import ModelUsageRequest, ThinkingTest, User
from .shared_providers import lock_provider
from .site_audit import record
from .site_auth import site_admin
from .thinking import ThinkingConfig, select_thinking
from .workspace_billing import LegacyPriceMeter, finish_operation, legacy_call_usage, operation_snapshot

router = APIRouter(prefix="/site/admin/llm-providers", tags=["site-admin"])


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, strict=True)
    connection_id: UUID | None = None
    connection_version: int | None = Field(default=None, ge=1, strict=True)
    thinking: ThinkingConfig
    option_id: str | None = Field(default=None, min_length=1, max_length=40)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000, strict=True)


class TestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    version: int = Field(ge=1, strict=True)
    connection_version: int = Field(ge=1, strict=True)
    thinking_config_version: int = Field(ge=0, strict=True)
    option_id: str | None = Field(default=None, min_length=1, max_length=40)


def parameter_preview(config):
    try:
        provider = OpenAICompatibleChatProvider(config=config)
        if config.api_protocol in ("anthropic", "gemini"):
            from .model_runtime.native import request as native_request
            payload = native_request(provider, [], streaming=True)[2]
        else:
            payload = provider._payload([], stream=True)
    except LLMConfigurationError as exc:
        raise HTTPException(422, str(exc)) from None
    allowed = {"thinking", "reasoning_effort", "enable_thinking", "thinking_budget",
        "max_tokens", "max_completion_tokens", "max_output_tokens", "reasoning", "stream", "stream_options", "generationConfig"}
    return {key: value for key, value in payload.items() if key in allowed}


@router.post("/{identifier}/thinking/preview")
def preview(identifier: UUID, payload: PreviewInput, db=Depends(get_db), actor=Depends(site_admin)):
    row = lock_provider(db, identifier, payload.version)
    from .model_connections import lock_connection
    connection = lock_connection(db, payload.connection_id or row.connection_id, payload.connection_version)
    config = ChatProviderConfig("", "", row.model,
        thinking=select_thinking(payload.thinking.model_dump(), row.thinking_version, payload.option_id),
        max_output_tokens=payload.max_output_tokens, api_protocol=row.chat_protocol)
    return {"parameters": parameter_preview(config), "version": row.version,
        "thinking_version": row.thinking_version}


@router.post("/{identifier}/thinking/test")
def test_configuration(identifier: UUID, payload: TestInput, request: Request,
        db=Depends(get_db), actor=Depends(site_admin), access: Access = Depends(get_access)):
    db.get(User, actor.id, with_for_update=True)
    existing = db.get(ThinkingTest, payload.request_id, populate_existing=True)
    if existing:
        if (existing.actor_user_id != actor.id or existing.account_id != access.user_id
                or existing.provider_id != identifier or existing.version != payload.version
                or existing.connection_version != payload.connection_version
                or existing.thinking_version != payload.thinking_config_version
                or existing.option_id != payload.option_id):
            raise HTTPException(409, "测试请求已被使用，请重新发起")
        if existing.status == "running":
            raise HTTPException(409, "该测试正在进行，请稍后查看")
        return existing.result
    count = db.scalar(select(func.count()).select_from(ThinkingTest).where(
        ThinkingTest.actor_user_id == actor.id,
        ThinkingTest.created_at >= datetime.now(timezone.utc) - timedelta(minutes=1)))
    if count >= 3:
        raise HTTPException(429, "测试过于频繁，请一分钟后重试")
    # Reject IDs already used by a normal model request before admitting a test.
    from .models import BillingSnapshot, ChatMessage, ContextOperation
    if (db.get(ModelUsageRequest, payload.request_id) or db.get(ContextOperation, payload.request_id)
            or db.scalar(select(ChatMessage.id).where(ChatMessage.request_id == payload.request_id))
            or db.scalar(select(BillingSnapshot.id).where(BillingSnapshot.request_id == payload.request_id))):
        raise HTTPException(409, "测试请求已被使用，请重新发起")
    config = request_provider_config(db, access.user_id, identifier, payload.option_id,
        payload.thinking_config_version)
    if config.config_version != payload.version or config.connection_version != payload.connection_version:
        raise HTTPException(409, "供应商已更新，请重新加载后测试")
    config = replace(config, max_output_tokens=min(config.max_output_tokens or 1024, 1024),
        timeout_seconds=min(config.timeout_seconds, 90))
    metered = model_billing.enabled(db)
    hold = authorization_hold_amount()
    if metered:
        model_billing.start_request(db, access.user_id, actor.id, payload.request_id,
            UUID(config.provider_id), hold)
    else:
        model_billing.settle_pending(db, access.user_id, hold)
        operation_snapshot(db, access.user_id, payload.request_id, "model",
            capabilities=model_billing.CAPABILITIES, provider_id=UUID(config.provider_id))
        authorization_hold(db, access.user_id, actor.id, payload.request_id, hold)
    row = ThinkingTest(request_id=payload.request_id, actor_user_id=actor.id, account_id=access.user_id,
        provider_id=identifier, version=payload.version, connection_version=config.connection_version,
        thinking_version=payload.thinking_config_version,
        option_id=payload.option_id, status="running", result={})
    db.add(row)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    meter = (model_billing.ModelMeter(factory, payload.request_id) if metered else
        LegacyPriceMeter(factory, access.user_id, payload.request_id))
    provider = OpenAICompatibleChatProvider(config=config)
    messages = [{"role": "user", "content": "计算17乘23，只输出结果。"}]
    result = {"request_id": str(payload.request_id), "version": payload.version,
        "connection_version": config.connection_version,
        "thinking_version": payload.thinking_config_version, "option_id": payload.option_id,
        "parameters": parameter_preview(config), "completed": False, "error": None}
    try:
        def invoke():
            response = None
            for name, data in provider.stream_with_tools(messages, []):
                if name == "complete":
                    response = data
            if response is None:
                raise LLMProviderError("模型未完整返回，请检查配置后重试")
            return response
        meter.invoke(provider, invoke, messages, [])
        result["completed"] = True
    except (LLMProviderError, LLMConfigurationError):
        result["error"] = "测试未完整完成，请核对接口支持的思考参数及输出限制"
    except Exception:
        result["error"] = "测试未能完成，请稍后重试"
    finally:
        result.update(provider.thinking_diagnostic())
        if provider.last_usage:
            result["usage"] = provider.last_usage.measured()
        with factory() as settled:
            if metered:
                credits = model_billing.finalize_request(settled, payload.request_id,
                    "completed" if result["completed"] else "failed")
            else:
                partition = access.user_id
                credits = settle_model_usage(settled, partition, actor.id, payload.request_id,
                    legacy_call_usage(settled, partition, payload.request_id), hold)
                finish_operation(settled, partition, payload.request_id)
            result["credits"] = float(credits)
            test = settled.get(ThinkingTest, payload.request_id, with_for_update=True)
            test.status = "completed" if result["completed"] else "failed"
            test.result = result
            record(settled, actor, "models.thinking_tested", "model_provider", identifier, request=request,
                result="success" if result["completed"] else "failure",
                details={"request_id": str(payload.request_id), "version": payload.version,
                    "option_id": payload.option_id, "received_thinking": result["received_thinking"],
                    "reasoning_tokens": result["reasoning_tokens"]})
            settled.commit()
    return result
