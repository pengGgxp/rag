"""Durable per-call measurement with one exact request-level settlement."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .credits import CreditError, _entry, authorization_hold, ensure_account, release_hold
from .model_runtime.client import measured_usage
from .models import ModelBillingSettings, ModelCallUsage, ModelUsageRequest, SearchToolCall, UsageEvent
from .workspace_billing import context_for_workspace, resolve_prices, multiplier, allocate, finish_operation
from .credit_amounts import exact_charge

CAPABILITIES = ("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "completion_tokens")


def enabled(db):
    row = db.get(ModelBillingSettings, 1, populate_existing=True)
    return bool(row and row.enabled)


def price_snapshot(db, account_id, provider_id=None, *, context=None):
    return resolve_prices(db, account_id, CAPABILITIES, provider_id, context=context)


def pending_credits(db, account_id):
    from decimal import Decimal
    return db.scalar(select(func.coalesce(func.sum(ModelUsageRequest.credits), 0)).where(
        ModelUsageRequest.account_id == account_id, ModelUsageRequest.status == "pending")) or Decimal(0)


def _pay(db, account, row):
    if row.credits:
        _entry(db, account, actor_user_id=row.actor_user_id, amount=-row.credits,
            kind="usage_settlement", idempotency_key=f"model:{row.request_id}:charge",
            metadata={"request_id": str(row.request_id), "capability": "model_tokens"})
    row.status = "settled"


def settle_pending(db, account_id, hold=0):
    account = ensure_account(db, account_id)
    debts = db.scalars(select(ModelUsageRequest).where(
        ModelUsageRequest.account_id == account_id, ModelUsageRequest.status == "pending"
    ).order_by(ModelUsageRequest.created_at, ModelUsageRequest.request_id).with_for_update()).all()
    if account.balance < sum(row.credits for row in debts) + hold:
        raise CreditError()
    for row in debts:
        _pay(db, account, row)


def start_request(db, account_id, actor_user_id, request_id, provider_id, hold, *, search_config=None, tool_only=False):
    account = ensure_account(db, account_id)
    existing = db.get(ModelUsageRequest, request_id, populate_existing=True)
    if existing:
        raise HTTPException(409, "模型请求已存在，请勿重复提交")
    context = context_for_workspace(db, account_id)
    prices = {} if tool_only else price_snapshot(db, account_id, provider_id, context=context)
    context.update(model_provider_id=str(provider_id), model_prices=prices)
    if search_config:
        context["search"] = search_config.snapshot()
    debts = db.scalars(select(ModelUsageRequest).where(
        ModelUsageRequest.account_id == account_id, ModelUsageRequest.status == "pending"
    ).order_by(ModelUsageRequest.created_at, ModelUsageRequest.request_id).with_for_update()).all()
    if account.balance < sum(row.credits for row in debts) + hold:
        raise CreditError()
    for row in debts:
        _pay(db, account, row)
    authorization_hold(db, account_id, actor_user_id, request_id, hold)
    db.add(ModelUsageRequest(request_id=request_id, account_id=account_id,
        actor_user_id=actor_user_id, hold=hold, billing_context=context,
        formula_version="model_tools_decimal_v1" if search_config else "model_cache_decimal_v1"))
    db.flush()


def _request_locked(db, request_id):
    row = db.get(ModelUsageRequest, request_id)
    if row is None:
        raise ValueError("unknown model request")
    account = ensure_account(db, row.account_id)
    db.refresh(row, with_for_update=True)
    return row, account


def begin_call(db, request_id, provider):
    row, _ = _request_locked(db, request_id)
    if row.status != "running":
        raise HTTPException(409, "模型请求已经结束")
    status = provider.status
    provider_id = UUID(status.provider_id) if status.provider_id else None
    prices = price_snapshot(db, row.account_id, provider_id,
        context=row.billing_context or {"source_mode": "legacy", "multiplier_percent": 100})
    round_number = db.scalar(select(func.count()).select_from(ModelCallUsage).where(
        ModelCallUsage.request_id == request_id))
    call = ModelCallUsage(id=uuid4(), request_id=request_id, round=round_number,
        provider_id=provider_id, provider_key=status.provider_key,
        model=status.model or "unknown", prices=prices)
    db.add(call)
    db.flush()
    return call.id


def finish_call(db, request_id, call_id, usage, status):
    row, _ = _request_locked(db, request_id)
    call = db.get(ModelCallUsage, call_id)
    if call is None or call.request_id != request_id:
        raise ValueError("model call scope mismatch")
    if row.status != "running" or call.status != "started":
        return
    call.usage, call.status = usage, status
    db.flush()


def finalize_request(db, request_id, outcome):
    row, account = _request_locked(db, request_id)
    if row.status != "running":
        return row.credits
    calls = db.scalars(select(ModelCallUsage).where(
        ModelCallUsage.request_id == request_id).order_by(ModelCallUsage.round)).all()
    parts = []
    for call in calls:
        if call.status == "started":
            call.status = "interrupted"
        for order, (capability, field) in enumerate(zip(CAPABILITIES, USAGE_FIELDS)):
            units = call.usage.get(field)
            if field == "cached_input_tokens" and units is None and call.usage.get("input_tokens") is not None:
                units = 0
            if type(units) is not int or units < 0:
                continue
            price = call.prices[capability]
            exact = exact_charge(units, price) * multiplier(row.billing_context)
            parts.append((call, capability, units, exact, order))
    tool_calls = db.scalars(select(SearchToolCall).where(SearchToolCall.request_id == request_id)
        .order_by(SearchToolCall.created_at, SearchToolCall.id)).all()
    tool_parts = []
    for tool in tool_calls:
        if tool.status == "started":
            tool.status = "interrupted"
        if tool.units:
            tool_parts.append((tool, exact_charge(tool.units, tool.price) * multiplier(row.billing_context)))
    total, allocations = allocate([item[3] for item in parts] + [item[1] for item in tool_parts])
    for (call, capability, units, exact, _), credits in zip(parts, allocations[:len(parts)]):
        price = call.prices[capability]
        db.add(UsageEvent(account_id=row.account_id, actor_user_id=row.actor_user_id,
            request_id=request_id, provider_id=call.provider_id, provider_key=call.provider_key,
            capability=capability, units=units, credits=credits, policy_version=price["version"],
            policy_snapshot={**price, "capability": capability, "formula_version": row.formula_version,
                "model": call.model, "provider_id": str(call.provider_id) if call.provider_id else None,
                **{key: row.billing_context.get(key, default) for key, default in (
                    ("source_mode", "legacy"), ("multiplier_percent", 100), ("multiplier_version", 0))}},
            idempotency_key=f"usage:{request_id}:model:{call.round}:{capability}",
            metadata_json={"call_id": str(call.id), "round": call.round,
                "source": call.usage.get("source"), "cache_status": call.usage.get("cache_status"),
                "exact_numerator": str(exact.numerator), "exact_denominator": str(exact.denominator)}))
    for (tool, exact), credits in zip(tool_parts, allocations[len(parts):]):
        db.add(UsageEvent(account_id=row.account_id, actor_user_id=row.actor_user_id,
            request_id=request_id, capability=tool.capability, units=tool.units, credits=credits,
            policy_version=tool.price["version"], policy_snapshot={**tool.price,
                "formula_version": row.formula_version,
                "multiplier_percent": row.billing_context.get("multiplier_percent", 100),
                "multiplier_version": row.billing_context.get("multiplier_version", 0)},
            idempotency_key=f"usage:{request_id}:tool:{tool.id}",
            metadata_json={"call_id": str(tool.id), "source": "deepseek_search",
                "exact_numerator": str(exact.numerator), "exact_denominator": str(exact.denominator)}))
    row.credits, row.outcome = total, outcome
    finish_operation(db, row.account_id, request_id)
    row.finalized_at = datetime.now(timezone.utc)
    release_hold(db, row.account_id, row.actor_user_id, request_id, row.hold)
    if account.balance >= total:
        _pay(db, account, row)
    else:
        row.status = "pending"
    db.flush()
    return total


def recover_requests(db):
    from .models import ConversationExecution
    identifiers = db.scalars(select(ModelUsageRequest.request_id).where(
        ModelUsageRequest.status == "running", ~ModelUsageRequest.request_id.in_(
            select(ConversationExecution.request_id).where(ConversationExecution.status.in_(["queued", "running"]))))).all()
    for identifier in identifiers:
        finalize_request(db, identifier, "interrupted")
    db.commit()


class ModelMeter:
    def __init__(self, factory, request_id):
        self.factory, self.request_id = factory, request_id

    def invoke(self, provider, operation, messages, tools=None):
        with self.factory() as db:
            call_id = begin_call(db, self.request_id, provider)
            db.commit()
        provider.last_usage = None
        try:
            response = operation()
        except BaseException:
            with self.factory() as db:
                finish_call(db, self.request_id, call_id,
                    {**measured_usage(provider.last_usage, messages, {}, tools, success=False),
                     "thinking_diagnostic": provider.thinking_diagnostic() if hasattr(provider, "thinking_diagnostic") else {}}, "failed")
                db.commit()
            raise
        if isinstance(response, str):
            output, success = {"content": response}, bool(response)
        else:
            output = {"content": response.content, "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments} for call in response.tool_calls]}
            success = bool(response.content or response.tool_calls)
        usage = measured_usage(getattr(response, "usage", None) or provider.last_usage,
            messages, output, tools, success=success)
        if hasattr(provider, "thinking_diagnostic"):
            usage["thinking_diagnostic"] = provider.thinking_diagnostic()
        if getattr(provider, "stream_diagnostic", None) == "upstream_returned_json":
            usage["stream_diagnostic"] = "upstream_returned_json"
        with self.factory() as db:
            finish_call(db, self.request_id, call_id, usage, "completed" if success else "failed")
            db.commit()
        return response
