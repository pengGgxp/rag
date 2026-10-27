"""Durable per-call measurement with one exact request-level settlement."""
from datetime import datetime, timezone
from fractions import Fraction
from math import ceil
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .credits import CreditError, _entry, authorization_hold, ensure_account, release_hold
from .llm import measured_usage
from .models import (ModelBillingSettings, ModelCallUsage, ModelUsageRequest, UsageEvent,
                     UsagePolicy)

CAPABILITIES = ("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens")
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "completion_tokens")


def enabled(db):
    row = db.get(ModelBillingSettings, 1, populate_existing=True)
    return bool(row and row.enabled)


def price_snapshot(db, workspace_id, provider_id=None):
    rows = db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id,
        UsagePolicy.provider_id == provider_id, UsagePolicy.capability.in_(CAPABILITIES))).all()
    by_capability = {row.capability: row for row in rows}
    if len(by_capability) != len(CAPABILITIES) or any(not row.enabled for row in rows):
        raise HTTPException(503, "模型服务暂不可用，请联系站点管理员完善计费配置")
    return {key: {"unit": by_capability[key].unit,
        "unit_size": by_capability[key].unit_size,
        "credits_per_unit": by_capability[key].credits_per_unit,
        "version": by_capability[key].version} for key in CAPABILITIES}


def pending_credits(db, workspace_id):
    return int(db.scalar(select(func.coalesce(func.sum(ModelUsageRequest.credits), 0)).where(
        ModelUsageRequest.workspace_id == workspace_id, ModelUsageRequest.status == "pending")) or 0)


def _pay(db, account, row):
    if row.credits:
        _entry(db, account, actor_user_id=row.actor_user_id, amount=-row.credits,
            kind="usage_settlement", idempotency_key=f"model:{row.request_id}:charge",
            metadata={"request_id": str(row.request_id), "capability": "model_tokens"})
    row.status = "settled"


def start_request(db, workspace_id, actor_user_id, request_id, provider_id, hold):
    account = ensure_account(db, workspace_id)
    existing = db.get(ModelUsageRequest, request_id, populate_existing=True)
    if existing:
        raise HTTPException(409, "模型请求已存在，请勿重复提交")
    price_snapshot(db, workspace_id, provider_id)
    debts = db.scalars(select(ModelUsageRequest).where(
        ModelUsageRequest.workspace_id == workspace_id, ModelUsageRequest.status == "pending"
    ).order_by(ModelUsageRequest.created_at, ModelUsageRequest.request_id).with_for_update()).all()
    if account.balance < sum(row.credits for row in debts) + hold:
        raise CreditError()
    for row in debts:
        _pay(db, account, row)
    authorization_hold(db, workspace_id, actor_user_id, request_id, hold)
    db.add(ModelUsageRequest(request_id=request_id, workspace_id=workspace_id,
        actor_user_id=actor_user_id, hold=hold))
    db.flush()


def _request_locked(db, request_id):
    row = db.get(ModelUsageRequest, request_id)
    if row is None:
        raise ValueError("unknown model request")
    account = ensure_account(db, row.workspace_id)
    db.refresh(row, with_for_update=True)
    return row, account


def begin_call(db, request_id, provider):
    row, _ = _request_locked(db, request_id)
    if row.status != "running":
        raise HTTPException(409, "模型请求已经结束")
    status = provider.status
    provider_id = UUID(status.provider_id) if status.provider_id else None
    prices = price_snapshot(db, row.workspace_id, provider_id)
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
            exact = Fraction(units * price["credits_per_unit"], price["unit_size"])
            parts.append((call, capability, units, exact, order))
    total = ceil(sum((item[3] for item in parts), Fraction()))
    allocations = [item[3].numerator // item[3].denominator for item in parts]
    remaining = total - sum(allocations)
    ranked = sorted(range(len(parts)), key=lambda i: (
        -(parts[i][3]-allocations[i]), parts[i][0].round, parts[i][4]))
    for index in ranked[:remaining]:
        allocations[index] += 1
    for (call, capability, units, exact, _), credits in zip(parts, allocations):
        price = call.prices[capability]
        db.add(UsageEvent(workspace_id=row.workspace_id, actor_user_id=row.actor_user_id,
            request_id=request_id, provider_id=call.provider_id, provider_key=call.provider_key,
            capability=capability, units=units, credits=credits, policy_version=price["version"],
            policy_snapshot={**price, "capability": capability, "formula_version": row.formula_version,
                "model": call.model, "provider_id": str(call.provider_id) if call.provider_id else None},
            idempotency_key=f"usage:{request_id}:model:{call.round}:{capability}",
            metadata_json={"call_id": str(call.id), "round": call.round,
                "source": call.usage.get("source"), "cache_status": call.usage.get("cache_status"),
                "exact_numerator": str(exact.numerator), "exact_denominator": str(exact.denominator)}))
    row.credits, row.outcome = total, outcome
    row.finalized_at = datetime.now(timezone.utc)
    release_hold(db, row.workspace_id, row.actor_user_id, request_id, row.hold)
    if account.balance >= total:
        _pay(db, account, row)
    else:
        row.status = "pending"
    db.flush()
    return total


def recover_requests(db):
    identifiers = db.scalars(select(ModelUsageRequest.request_id).where(
        ModelUsageRequest.status == "running")).all()
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
                    measured_usage(provider.last_usage, messages, {}, tools, success=False), "failed")
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
        with self.factory() as db:
            finish_call(db, self.request_id, call_id, usage, "completed" if success else "failed")
            db.commit()
        return response
