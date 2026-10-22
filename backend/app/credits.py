from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import Access, get_access
from .config import get_settings
from .models import CreditAccount, CreditLedgerEntry, UsageEvent, UsagePolicy

INITIAL_CREDITS = 1000
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
    unit_size: int = Field(default=1, ge=1)
    credits_per_unit: int = Field(default=0, ge=0)
    enabled: bool = True


def ensure_policies(db: Session, workspace_id: UUID) -> list[UsagePolicy]:
    existing = {
        policy.capability: policy
        for policy in db.scalars(select(UsagePolicy).where(
            UsagePolicy.workspace_id == workspace_id,
        )).all()
    }
    for defaults in DEFAULT_USAGE_POLICIES:
        if defaults["capability"] not in existing:
            policy = UsagePolicy(workspace_id=workspace_id, **defaults)
            db.add(policy)
            existing[policy.capability] = policy
    db.flush()
    return list(existing.values())


def ensure_account(db: Session, workspace_id: UUID, *, initial: int = INITIAL_CREDITS) -> CreditAccount:
    account = db.get(CreditAccount, workspace_id, with_for_update=True, populate_existing=True)
    if account is not None:
        return account
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
            metadata_json={"reason": "workspace_created"},
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
    policies = ensure_policies(db, workspace_id)
    policy = next((item for item in policies if item.capability == capability), None)
    if policy is None or not policy.enabled:
        raise CreditError()
    credits = ceil(units / policy.unit_size) * policy.credits_per_unit if units else 0
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
        capability=capability, units=units, credits=credits,
        policy_version=policy.version,
        policy_snapshot={
            "capability": policy.capability, "unit": policy.unit,
            "unit_size": policy.unit_size, "credits_per_unit": policy.credits_per_unit,
            "enabled": policy.enabled,
        },
        idempotency_key=idempotency_key,
        metadata_json=metadata or {},
    )
    db.add(event)
    db.flush()
    return event


def settle_model_usage(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    request_id: UUID,
    usage_events: list[dict],
    hold: int = CREDIT_RESERVATION,
) -> int:
    total = 0
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
            )
            total += event.credits
    reconcile_hold(db, workspace_id, actor_user_id, request_id, total, hold)
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

    @router.get("/policies")
    def read_policies(db: Session = Depends(get_db), access: Access = Depends(get_access)):
        access.admin()
        policies = ensure_policies(db, access.workspace_id)
        db.commit()
        return [{
            "capability": item.capability, "unit": item.unit,
            "unit_size": item.unit_size, "credits_per_unit": item.credits_per_unit,
            "enabled": item.enabled, "version": item.version,
        } for item in sorted(policies, key=lambda row: row.capability)]

    @router.put("/policies/{capability}")
    def update_policy(capability: str, payload: UsagePolicyUpdate,
                      db: Session = Depends(get_db), access: Access = Depends(get_access)):
        access.admin()
        if capability != payload.capability:
            raise HTTPException(422, "能力标识不一致")
        policies = ensure_policies(db, access.workspace_id)
        policy = next((item for item in policies if item.capability == capability), None)
        if policy is None:
            policy = UsagePolicy(
                workspace_id=access.workspace_id,
                capability=capability,
                unit=payload.unit,
                unit_size=payload.unit_size,
                credits_per_unit=payload.credits_per_unit,
                enabled=payload.enabled,
                updated_by=access.user_id,
            )
            db.add(policy)
            db.flush()
        else:
            policy.version += 1
            policy.updated_by = access.user_id
            policy.unit = payload.unit
            policy.unit_size = payload.unit_size
            policy.credits_per_unit = payload.credits_per_unit
            policy.enabled = payload.enabled
        db.commit()
        return {
            "capability": policy.capability, "unit": policy.unit,
            "unit_size": policy.unit_size, "credits_per_unit": policy.credits_per_unit,
            "enabled": policy.enabled, "version": policy.version,
        }

    @router.post("/credits")
    def add_credits(access: Access = Depends(get_access)):
        raise HTTPException(403, "不支持直接增加积分，请使用积分充值")

    return router
