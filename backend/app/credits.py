from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import Access, get_access
from .audit_log import record_audit
from .models import CreditAccount, CreditLedgerEntry

CREDIT_RESERVATION = 10
INITIAL_CREDITS = 1000


class CreditError(HTTPException):
    def __init__(self):
        super().__init__(status_code=402, detail="积分余额不足，请联系管理员充值")


class CreditGrantRequest(BaseModel):
    amount: int = Field(ge=1, le=1_000_000)
    note: str = Field(default="管理员充值", max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)


def ensure_account(db: Session, workspace_id: UUID, *, initial: int = INITIAL_CREDITS) -> CreditAccount:
    account = db.get(CreditAccount, workspace_id, with_for_update=True)
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
    return entry


def grant(
    db: Session,
    workspace_id: UUID,
    actor_user_id: UUID,
    amount: int,
    note: str,
    idempotency_key: str,
) -> CreditLedgerEntry:
    account = ensure_account(db, workspace_id, initial=0)
    return _entry(
        db,
        account,
        actor_user_id=actor_user_id,
        amount=amount,
        kind="admin_grant",
        idempotency_key=idempotency_key,
        metadata={"note": note},
    )


def build_router(get_db):
    router = APIRouter(prefix="/usage", tags=["usage"])

    @router.get("")
    def read_usage(db: Session = Depends(get_db), access: Access = Depends(get_access)):
        account = ensure_account(db, access.workspace_id)
        db.commit()
        entries = db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == access.workspace_id,
        ).order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc()).limit(20)).all()
        return {
            "balance": account.balance,
            "lifetime_granted": account.lifetime_granted,
            "lifetime_spent": account.lifetime_spent,
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

    @router.post("/credits")
    def add_credits(
        payload: CreditGrantRequest,
        request: Request,
        db: Session = Depends(get_db),
        access: Access = Depends(get_access),
    ):
        access.admin()
        entry = grant(
            db,
            access.workspace_id,
            access.user_id,
            payload.amount,
            payload.note,
            f"admin:{access.workspace_id}:{payload.idempotency_key}",
        )
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="usage.credits_granted", resource_type="credit_account",
            resource_id=access.workspace_id, metadata={"amount": payload.amount, "note": payload.note},
            request=request,
        )
        db.commit()
        return {
            "amount": entry.amount,
            "balance_after": entry.balance_after,
            "kind": entry.kind,
        }

    return router
