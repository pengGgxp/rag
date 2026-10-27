from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.credits import (
    CreditError, authorization_hold, release, reserve, settle_model_usage,
)
from app.models import CreditAccount, CreditLedgerEntry, UsageEvent, User
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


def test_model_usage_is_metered_by_configured_policy_and_hold_is_released(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    workspace_id = UUID(session["workspace_id"])
    user_id = UUID(session["user"]["id"])
    request_id = uuid4()
    authorization_hold(db, workspace_id, user_id, request_id, amount=1)
    from app.models import UsagePolicy
    db.add_all([UsagePolicy(workspace_id=workspace_id, capability=key, unit="tokens", unit_size=1000, credits_per_unit=rate)
        for key, rate in (("llm_input_tokens", 1), ("llm_output_tokens", 3))])
    db.flush()
    settle_model_usage(db, workspace_id, user_id, request_id, [{
        "prompt_tokens": 1500,
        "completion_tokens": 600,
        "source": "provider",
    }], hold=1)
    db.commit()

    account = db.get(CreditAccount, workspace_id)
    assert account.balance == 995
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
    assert {(event.capability, event.units, event.credits) for event in events} == {
        ("llm_input_tokens", 1500, 2),
        ("llm_output_tokens", 600, 3),
    }
    assert all(event.policy_snapshot["unit_size"] == 1000 for event in events)

    # Replaying settlement is idempotent and cannot charge the workspace twice.
    authorization_hold(db, workspace_id, user_id, request_id, amount=1)
    settle_model_usage(db, workspace_id, user_id, request_id, [{
        "prompt_tokens": 1500,
        "completion_tokens": 600,
        "source": "provider",
    }], hold=1)
    db.commit()
    assert db.get(CreditAccount, workspace_id).balance == 995


def test_only_site_admin_can_manage_policy_but_users_do_not_see_formula(accounts):
    (owner, viewer, _), db = accounts
    session = register(owner)
    assert owner.get("/usage/policies").status_code == 403
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    assert owner.get("/usage/policies").status_code == 200
    updated = owner.put("/usage/policies/embedding_tokens", json={
        "capability": "embedding_tokens",
        "unit": "tokens",
        "unit_size": 500,
        "credits_per_unit": 4,
        "enabled": True,
    })
    assert updated.status_code == 410
    usage = owner.get("/usage").json()
    assert "credits_per_unit" not in usage
