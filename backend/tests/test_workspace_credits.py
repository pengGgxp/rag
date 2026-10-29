from uuid import UUID, uuid4
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import CreditAccount, CreditLedgerEntry, ModelUsageRequest, SiteAuditEvent, UsageEvent
from test_site_admin import setup


def payload(**changes):
    return {"request_id": str(uuid4()), "direction": "add", "amount": 200,
            "expected_balance": 1000, "reason": "客服补发", **changes}


def endpoint(workspace):
    return f"/site/admin/workspaces/{workspace}/credits"


def test_admin_adjustments_share_balance_and_preserve_paid_orders_and_pending(accounts):
    admin, member, _, db, a, b = setup(accounts)
    wid = b["workspace_id"]
    db.add(ModelUsageRequest(request_id=uuid4(), workspace_id=UUID(wid), status="pending",
        credits=20, hold=0, outcome="confirmed_usage")); db.commit()
    result = admin.post(endpoint(wid) + "/adjustments", json=payload())
    assert result.status_code == 200, result.text
    assert result.json()["credits"] == {"balance": 1200, "lifetime_granted": 1200, "lifetime_spent": 0}
    assert member.get("/usage").json()["balance"] == 1200
    result = admin.post(endpoint(wid) + "/adjustments", json=payload(direction="deduct", amount=1200, expected_balance=1200))
    assert result.status_code == 200 and result.json()["credits"]["balance"] == 0
    history = admin.get(endpoint(wid)).json()
    assert history["pending_credits"] == 20 and len(history["adjustments"]) == 2
    assert {row["amount"] for row in history["adjustments"]} == {200, -1200}
    assert all(row["actor_name"] == "Owner" and row["reason"] == "客服补发" for row in history["adjustments"])
    public = member.get("/usage").json()
    assert all(row["metadata"] == {} for row in public["entries"] if row["kind"].startswith("manual_"))
    assert "客服补发" not in member.get("/usage").text
    assert admin.get("/auth/me").json()["workspace_id"] == a["workspace_id"]
    assert admin.get("/usage").json()["balance"] == 1000
    assert db.scalar(select(func.count()).select_from(UsageEvent)) == 0
    audit = admin.get("/site/admin/audit?action=workspace.credits_adjusted").json()
    assert audit["total"] == 2 and all(row["target_id"] == wid for row in audit["items"])
    assert "客服补发" not in str(audit)
    assert sorted(Decimal(row["details"]["amount"]) for row in audit["items"]) == [-1200, 200]


def test_permissions_csrf_and_role_revocation(accounts):
    admin, member, anon, db, a, b = setup(accounts)
    path = endpoint(b["workspace_id"])
    for client, code in ((member, 403), (anon, 401)):
        assert client.get(path).status_code == code
        assert client.post(path + "/adjustments", json=payload()).status_code == code
    assert admin.post(path + "/adjustments", json=payload(), headers={"X-CSRF-Token": "invalid"}).status_code == 403
    from app.models import User
    db.get(User, UUID(a["user"]["id"])).is_site_admin = False; db.commit()
    assert admin.post(path + "/adjustments", json=payload()).status_code == 403
    assert member.get("/usage").json()["balance"] == 1000


@pytest.mark.parametrize("changes", [
    {"amount": 0}, {"amount": -1}, {"amount": True}, {"amount": 1.50001}, {"amount": "invalid"},
    {"amount": 2147483648}, {"direction": "other"}, {"reason": "  "}, {"reason": "x" * 201},
    {"expected_balance": -1}, {"expected_balance": True}, {"workspace_id": str(uuid4())},
])
def test_invalid_input_does_not_change_balance(accounts, changes):
    admin, member, _, db, _, b = setup(accounts)
    assert admin.post(endpoint(b["workspace_id"]) + "/adjustments", json=payload(**changes)).status_code == 422
    assert member.get("/usage").json()["balance"] == 1000
    assert not db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.action == "workspace.credits_adjusted")).all()


def test_idempotent_retry_and_conflicting_reuse(accounts):
    admin, member, _, db, _, b = setup(accounts)
    path = endpoint(b["workspace_id"]) + "/adjustments"
    data = payload()
    first = admin.post(path, json=data)
    second = admin.post(path, json=data)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    for changes in ({"amount": 201}, {"reason": "不同原因"}, {"direction": "deduct"}):
        assert admin.post(path, json={**data, **changes}).status_code == 409
    assert member.get("/usage").json()["balance"] == 1200
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(CreditLedgerEntry.kind == "manual_credit")) == 1
    assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(SiteAuditEvent.action == "workspace.credits_adjusted")) == 1


def test_balance_conflict_overdraw_overflow_missing_and_read_without_grant(accounts):
    admin, member, _, db, _, b = setup(accounts)
    path = endpoint(b["workspace_id"])
    for changes, code in (({"expected_balance": 900}, 409),
            ({"direction": "deduct", "amount": 1001}, 422), ({"amount": 2147483647}, 422)):
        assert admin.post(path + "/adjustments", json=payload(**changes)).status_code == code
    assert member.get("/usage").json()["balance"] == 1000
    assert admin.get(endpoint(uuid4())).status_code == 404
    assert admin.post(endpoint(uuid4()) + "/adjustments", json=payload()).status_code == 404
    from app.models import Workspace
    workspace = Workspace(name="No credit account")
    db.add(workspace); db.commit()
    assert admin.get(endpoint(workspace.id)).json()["credits"]["balance"] == 0
    assert db.get(CreditAccount, workspace.id) is None
    assert admin.post(endpoint(workspace.id) + "/adjustments", json=payload(expected_balance=0)).json()["credits"]["balance"] == 200
    assert len(db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.workspace_id == workspace.id)).all()) == 1
