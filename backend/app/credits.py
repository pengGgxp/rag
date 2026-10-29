from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import Access, get_access
from .config import get_settings
from .models import CreditAccount, CreditLedgerEntry, UsageEvent, UsagePolicy
from .management_access import ManagementAccess, management_access

site_router = APIRouter(prefix="/site/admin/workspaces/{workspace_id}/usage-policies", tags=["site-admin"])

CREDIT_RESERVATION = 1


def authorization_hold_amount() -> int:
    return get_settings().usage_authorization_hold_credits
DEFAULT_USAGE_POLICIES = (
    {"capability": "llm_input_tokens", "unit": "tokens", "unit_size": 1000, "credits_per_unit": 1},
    {"capability": "llm_output_tokens", "unit": "tokens", "unit_size": 1000, "credits_per_unit": 3},
    {"capability": "embedding_tokens", "unit": "tokens", "unit_size": 1000, "credits_per_unit": 1},
    {"capability": "ocr_pages", "unit": "pages", "unit_size": 1, "credits_per_unit": 10},
)


class CreditError(HTTPException):
    def __init__(self):
        super().__init__(status_code=402, detail="积分余额不足，请前往账号的积分与充值页面充值")


class UsagePolicyUpdate(BaseModel):
    capability: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=40)
    unit_size: int = Field(default=1, ge=1, le=2_147_483_647, strict=True)
    credits_per_unit: int = Field(default=0, ge=0, le=2_147_483_647, strict=True)
    enabled: bool = True
    version: int = Field(default=0, ge=0)


def ensure_policies(
    db: Session, workspace_id: UUID, provider_id: UUID | None = None,
) -> list[UsagePolicy]:
    existing = {
        policy.capability: policy
        for policy in db.scalars(select(UsagePolicy).where(
            UsagePolicy.workspace_id == workspace_id,
            UsagePolicy.provider_id == provider_id,
        )).all()
    }
    return list(existing.values())


def require_policy(db, workspace_id, capability):
    from .workspace_billing import resolve_prices
    return resolve_prices(db, workspace_id, (capability,))[capability]


def ensure_account(db: Session, workspace_id: UUID, *, initial: int | None = None) -> CreditAccount:
    account = db.get(CreditAccount, workspace_id, with_for_update=True, populate_existing=True)
    if account is not None:
        return account
    from .site_settings import signup_grant
    grant, settings_version = signup_grant(db)
    initial = grant if initial is None else initial
    account = CreditAccount(
        workspace_id=workspace_id,
        balance=initial,
        lifetime_granted=initial,
        lifetime_spent=0,
    )
    db.add(account)
    db.flush()
    if initial:
        db.add(CreditLedgerEntry(
            workspace_id=workspace_id,
            actor_user_id=None,
            amount=initial,
            balance_after=initial,
            kind="initial_grant",
            idempotency_key=f"workspace:{workspace_id}:initial",
            metadata_json={"reason": "workspace_created", "site_settings_version": settings_version},
        ))
    return account


def _entry(
    db: Session,
    account: CreditAccount,
    *,
    actor_user_id: UUID | None,
    amount: int,
    kind: str,
    idempotency_key: str,
    metadata: dict,
) -> CreditLedgerEntry:
    existing = db.scalar(select(CreditLedgerEntry).where(
        CreditLedgerEntry.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        return existing
    if amount < 0 and account.balance < -amount:
        raise CreditError()
    account.balance += amount
    if amount > 0:
        account.lifetime_granted += amount
    else:
        account.lifetime_spent += -amount
    entry = CreditLedgerEntry(
        workspace_id=account.workspace_id,
        actor_user_id=actor_user_id,
        amount=amount,
        balance_after=account.balance,
        kind=kind,
        idempotency_key=idempotency_key,
        metadata_json=metadata,
    )
    db.add(entry)
    db.flush()
    return entry


def reserve(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID,
    request_id: UUID,
    amount: int = CREDIT_RESERVATION,
) -> CreditLedgerEntry:
    account = ensure_account(db, workspace_id)
    return _entry(
        db,
        account,
        actor_user_id=actor_user_id,
        amount=-amount,
        kind="chat_reservation",
        idempotency_key=f"chat:{request_id}:reserve",
        metadata={"request_id": str(request_id), "credits": amount},
    )


def release(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    amount: int = CREDIT_RESERVATION,
) -> CreditLedgerEntry:
    existing = db.scalar(select(CreditLedgerEntry).where(
        CreditLedgerEntry.idempotency_key == f"chat:{request_id}:release",
    ))
    if existing is not None:
        return existing
    account = ensure_account(db, workspace_id, initial=0)
    entry = _entry(
        db,
        account,
        actor_user_id=actor_user_id,
        amount=amount,
        kind="chat_release",
        idempotency_key=f"chat:{request_id}:release",
        metadata={"request_id": str(request_id), "credits": amount},
    )
    if entry.kind == "chat_release":
        account.lifetime_granted -= amount
        account.lifetime_spent -= amount
        db.flush()
    return entry


def release_hold(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    amount: int = CREDIT_RESERVATION,
) -> CreditLedgerEntry:
    existing = db.scalar(select(CreditLedgerEntry).where(
        CreditLedgerEntry.idempotency_key == f"usage:{request_id}:release",
    ))
    if existing is not None:
        return existing
    account = ensure_account(db, workspace_id, initial=0)
    entry = _entry(
        db, account, actor_user_id=actor_user_id, amount=amount,
        kind="usage_hold_release",
        idempotency_key=f"usage:{request_id}:release",
        metadata={"request_id": str(request_id), "internal": True},
    )
    account.lifetime_granted -= amount
    account.lifetime_spent -= amount
    db.flush()
    return entry


def authorization_hold(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID,
    request_id: UUID,
    amount: int = 1,
) -> CreditLedgerEntry:
    """Reserve a small internal hold; final billing is based on measured usage."""
    return _entry(
        db, ensure_account(db, workspace_id), actor_user_id=actor_user_id,
        amount=-amount, kind="usage_hold",
        idempotency_key=f"usage:{request_id}:hold",
        metadata={"request_id": str(request_id), "internal": True},
    )


def settle_usage(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    capability: str,
    units: int,
    *,
    metadata: dict | None = None,
    event_key: str | None = None,
    provider_id: UUID | None = None,
    provider_key: str | None = None,
    pricing_context: dict | None = None,
) -> UsageEvent:
    """Record one measured event and reconcile it against the internal hold."""
    if units < 0:
        raise ValueError("usage units cannot be negative")
    idempotency_key = f"usage:{request_id}:{event_key or capability}"
    existing = db.scalar(select(UsageEvent).where(
        UsageEvent.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        return existing
    from fractions import Fraction
    from .workspace_billing import operation_snapshot, multiplier, finish_operation
    if pricing_context is None:
        pricing_context = operation_snapshot(db, workspace_id, request_id, event_key or capability,
            capabilities=(capability,), provider_id=provider_id).context
    price = pricing_context["prices"][capability]
    base = ((units + price["unit_size"] - 1) // price["unit_size"]) * price["credits_per_unit"] if units else 0
    credits = ceil(Fraction(base) * multiplier(pricing_context))
    account = ensure_account(db, workspace_id)
    if credits:
        _entry(
            db, account, actor_user_id=actor_user_id, amount=-credits,
            kind="usage_settlement",
            idempotency_key=f"{idempotency_key}:charge",
            metadata={"request_id": str(request_id), "capability": capability, "units": units},
        )
    event = UsageEvent(
        workspace_id=workspace_id, actor_user_id=actor_user_id, request_id=request_id,
        provider_id=provider_id, provider_key=provider_key,
        capability=capability, units=units, credits=credits,
        policy_version=price["version"],
        policy_snapshot={
            **price, **{key: pricing_context[key] for key in (
                "source_mode", "multiplier_percent", "multiplier_version")},
            "formula_version": "legacy_event_multiplier_v1",
            "provider_id": str(provider_id) if provider_id else None,
            "provider_key": provider_key,
        },
        idempotency_key=idempotency_key,
        metadata_json=metadata or {},
    )
    db.add(event)
    finish_operation(db, workspace_id, request_id, event_key or capability)
    db.flush()
    return event


def settle_model_usage(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    usage_events: list[dict],
    hold: int = CREDIT_RESERVATION,
    provider_id: UUID | None = None,
    provider_key: str | None = None,
) -> int:
    if any(usage.get("billing_snapshot_id") for usage in usage_events):
        return settle_shared_legacy_model(db, workspace_id, actor_user_id, request_id, usage_events, hold)
    total = 0
    event_provider_id = provider_id
    event_provider_key = provider_key
    if usage_events and event_provider_id is None:
        raw_provider_id = usage_events[0].get("provider_id")
        try:
            event_provider_id = UUID(str(raw_provider_id)) if raw_provider_id else None
        except (TypeError, ValueError):
            event_provider_id = None
        event_provider_key = usage_events[0].get("provider_key")
    for index, usage in enumerate(usage_events):
        for capability, units in (
            ("llm_input_tokens", usage.get("prompt_tokens")),
            ("llm_output_tokens", usage.get("completion_tokens")),
        ):
            if not isinstance(units, int) or units < 0:
                continue
            event = settle_usage(
                db, workspace_id, actor_user_id, request_id, capability, units,
                event_key=f"model:{index}:{capability}",
                metadata={"source": usage.get("source", "unknown"), "round": index},
                provider_id=event_provider_id,
                provider_key=event_provider_key,
            )
            total += event.credits
    reconcile_hold(db, workspace_id, actor_user_id, request_id, total, hold)
    return total


def settle_shared_legacy_model(db, workspace_id, actor_user_id, request_id, usage_events, hold):
    from fractions import Fraction
    from .models import BillingSnapshot
    from .workspace_billing import allocate, multiplier, finish_operation
    account = ensure_account(db, workspace_id)
    charge_key = f"usage:{request_id}:legacy-model:charge"
    existing = db.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == charge_key))
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id,
        UsageEvent.workspace_id == workspace_id)).all()
    if existing or events:
        return sum(row.credits for row in events)
    parts = []
    for index, usage in enumerate(usage_events):
        snapshot = db.get(BillingSnapshot, UUID(usage["billing_snapshot_id"]))
        if snapshot is None or snapshot.workspace_id != workspace_id or snapshot.request_id != request_id:
            raise ValueError("billing snapshot scope mismatch")
        for capability, field in (("llm_input_tokens", "prompt_tokens"), ("llm_output_tokens", "completion_tokens")):
            units = usage.get(field)
            if type(units) is not int or units < 0:
                continue
            price = snapshot.context["prices"][capability]
            base = ((units + price["unit_size"] - 1) // price["unit_size"]) * price["credits_per_unit"]
            parts.append((index, capability, units, price, snapshot, Fraction(base) * multiplier(snapshot.context)))
    total, allocations = allocate([part[-1] for part in parts])
    # Return the reservation atomically before testing the full request debit.
    release_hold(db, workspace_id, actor_user_id, request_id, hold)
    if total and account.balance >= total:
        _entry(db, account, actor_user_id=actor_user_id, amount=-total, kind="usage_settlement",
            idempotency_key=charge_key, metadata={"request_id": str(request_id), "capability": "model_tokens"})
    elif total:
        from .models import ModelUsageRequest
        db.add(ModelUsageRequest(request_id=request_id, workspace_id=workspace_id,
            actor_user_id=actor_user_id, status="pending", credits=total, hold=0,
            outcome="confirmed_usage", formula_version="legacy_model_multiplier_v1"))
    for (index, capability, units, price, snapshot, exact), credits in zip(parts, allocations):
        usage = usage_events[index]
        db.add(UsageEvent(workspace_id=workspace_id, actor_user_id=actor_user_id, request_id=request_id,
            provider_id=UUID(usage["provider_id"]) if usage.get("provider_id") else None,
            provider_key=usage.get("provider_key"), capability=capability, units=units, credits=credits,
            policy_version=price["version"], policy_snapshot={**price, **{
                key: snapshot.context[key] for key in ("source_mode", "multiplier_percent", "multiplier_version")},
                "formula_version": "legacy_model_multiplier_v1"},
            idempotency_key=f"usage:{request_id}:model:{index}:{capability}",
            metadata_json={"round": index, "source": usage.get("source"), "snapshot_id": str(snapshot.id),
                "exact_numerator": str(exact.numerator), "exact_denominator": str(exact.denominator)}))
        snapshot.status = "complete"
    finish_operation(db, workspace_id, request_id)
    db.flush()
    return total


def reconcile_hold(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    actual_credits: int,
    hold: int = 1,
) -> None:
    # The hold authorizes the request; it is always returned after measured
    # usage is charged. The measured settlement is the only final debit.
    release_hold(db, workspace_id, actor_user_id, request_id, hold)


def recharge(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID,
    amount: int,
    order_id: UUID,
    amount_fen: int,
) -> CreditLedgerEntry:
    account = ensure_account(db, workspace_id, initial=0)
    return _entry(
        db,
        account,
        actor_user_id=actor_user_id,
        amount=amount,
        kind="paid_recharge",
        idempotency_key=f"recharge:{order_id}:paid",
        metadata={"order_id": str(order_id), "amount_fen": amount_fen},
    )


def build_router(get_db):
    router = APIRouter(prefix="/usage", tags=["usage"])

    @router.get("")
    def read_usage(db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .model_billing import pending_credits
        account = ensure_account(db, access.workspace_id)
        ensure_policies(db, access.workspace_id)
        db.commit()
        entries = db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == access.workspace_id,
            CreditLedgerEntry.kind.not_in(("usage_hold", "usage_hold_release", "usage_hold_reconciliation")),
        ).order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc()).limit(20)).all()
        usage_rows = db.execute(
            select(UsageEvent.capability, func.sum(UsageEvent.units), func.sum(UsageEvent.credits))
            .where(UsageEvent.workspace_id == access.workspace_id)
            .group_by(UsageEvent.capability)
        ).all()
        return {
            "balance": account.balance,
            "lifetime_granted": account.lifetime_granted,
            "lifetime_spent": account.lifetime_spent,
            "pending_credits": pending_credits(db, access.workspace_id),
            "usage": {
                "events": sum(1 for item in entries if item.kind == "usage_settlement"),
                "units": {
                    capability: {"units": int(units or 0), "credits": int(credits or 0)}
                    for capability, units, credits in usage_rows
                },
            },
            "entries": [
                {
                    "id": str(item.id),
                    "amount": item.amount,
                    "balance_after": item.balance_after,
                    "kind": item.kind,
                    "metadata": item.metadata_json,
                    "created_at": item.created_at.isoformat(),
                }
                for item in entries
            ],
        }

    @site_router.get("")
    @router.get("/policies")
    def read_policies(db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
        from .model_pricing import prices_read
        policies = ensure_policies(db, access.workspace_id)
        db.commit()
        from .model_pricing import DOCUMENT_CAPABILITIES
        result = prices_read([p for p in policies if p.capability in DOCUMENT_CAPABILITIES], all_model=False)
        for key in DOCUMENT_CAPABILITIES:
            if not any(p["capability"] == key for p in result):
                result.append({"capability": key, "unit": "pages" if key == "ocr_pages" else "tokens",
                    "unit_size": 1 if key == "ocr_pages" else 1000, "credits_per_unit": None,
                    "enabled": True, "configured": False, "version": 0})
        return result

    @site_router.put("/{capability}")
    @router.put("/policies/{capability}")
    def update_policy(capability: str, payload: UsagePolicyUpdate,
                      db: Session = Depends(get_db), access: ManagementAccess = Depends(management_access)):
        raise HTTPException(410, "工作区独立价格已停止编辑，请在站点后台维护共享基准价与工作区倍率")

    @router.post("/credits")
    def add_credits(access: Access = Depends(get_access)):
        raise HTTPException(403, "不支持直接增加积分，请使用积分充值")

    return router
