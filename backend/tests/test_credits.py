from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.credits import CreditError, release, reserve
from app.models import CreditAccount, CreditLedgerEntry
from test_auth import accounts, register

def test_workspace_admin_cannot_mint_credits(accounts):
    (owner, _, _), db = accounts
    register(owner)

    usage = owner.get("/usage")
    assert usage.status_code == 200, usage.text
    assert usage.json()["balance"] == 1000

    payload = {
        "amount": 250,
        "note": "测试充值",
        "idempotency_key": "test-grant-001",
    }
    first = owner.post("/usage/credits", json=payload)
    second = owner.post("/usage/credits", json=payload)
    assert first.status_code == 403, first.text
    assert second.status_code == 403, second.text
    assert owner.get("/usage").json()["balance"] == 1000
    assert db.scalar(select(CreditLedgerEntry).where(
        CreditLedgerEntry.kind == "admin_grant",
    )) is None


def test_credit_reservation_is_released_idempotently_and_cannot_overdraw(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    workspace_id = UUID(session["workspace_id"])
    user_id = UUID(session["user"]["id"])

    request_id = uuid4()
    entry = reserve(db, workspace_id, user_id, request_id, amount=1000)
    db.commit()
    assert entry.amount == -1000
    account = db.get(CreditAccount, workspace_id)
    assert account.balance == 0
    with pytest.raises(CreditError):
        reserve(db, workspace_id, user_id, uuid4(), amount=1)
    db.rollback()

    account = db.get(CreditAccount, workspace_id)
    released = release(db, workspace_id, user_id, request_id, amount=1000)
    duplicate = release(db, workspace_id, user_id, request_id, amount=1000)
    db.commit()
    assert released.id == duplicate.id
    assert db.get(CreditAccount, workspace_id).balance == 1000
