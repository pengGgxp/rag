from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import model_pricing as prices, llm_providers
from app.credits import ensure_policies
from app.models import ModelBillingSettings, ModelUsageRequest, SiteAuditEvent, UsagePolicy, User
from test_auth import accounts, register


def template(version=0, rate=4):
    return {"version": version, "policies": [{"capability": key, "unit": "tokens",
        "unit_size": 1000, "credits_per_unit": rate, "enabled": True} for key in prices.CAPABILITIES]}


def administrator(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    return owner, db, UUID(session["workspace_id"])


def test_template_permissions_completeness_zero_and_version(accounts):
    (owner, other, anonymous), db = accounts
    session = register(owner)
    assert owner.get("/site/admin/pricing-template").status_code == 403
    assert owner.put("/site/admin/pricing-template", json=template()).status_code == 403
    assert anonymous.get("/site/admin/pricing-template").status_code == 401
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    empty = owner.get("/site/admin/pricing-template").json()
    assert empty["configured"] is False and not empty["billing_enabled"]
    assert all(row["credits_per_unit"] is None and not row["configured"] for row in empty["policies"])
    invalid = template()
    invalid["policies"][1] = invalid["policies"][0]
    assert owner.put("/site/admin/pricing-template", json=invalid).status_code == 422
    assert owner.put("/site/admin/pricing-template", json=template(rate=0)).status_code == 200
    assert owner.get("/site/admin/pricing-template").json()["configured"]
    assert owner.put("/site/admin/pricing-template", json=template()).status_code == 409
    assert db.scalar(select(SiteAuditEvent).where(SiteAuditEvent.action == "billing.template_updated"))


@pytest.mark.parametrize("value", [True, -1, 0.5, None, "1"])
def test_template_rejects_unreliable_prices(accounts, value):
    owner, _, _ = administrator(accounts)
    payload = template()
    payload["policies"][1]["credits_per_unit"] = value
    assert owner.put("/site/admin/pricing-template", json=payload).status_code == 422


def test_workspace_template_is_transactional_one_time_copy(accounts, monkeypatch):
    owner, db, workspace = administrator(accounts)
    owner.put("/site/admin/pricing-template", json=template(rate=7))
    (_, other, _), _ = accounts
    second = register(other, "template@example.com", "Template")
    target = UUID(second["workspace_id"])
    copied = db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == target)).all()
    assert len(copied) == 3
    assert {row.credits_per_unit for row in copied} == {7}
    assert {row.template_version for row in copied} == {1}
    owner.put("/site/admin/pricing-template", json=template(version=1, rate=8))
    db.expire_all()
    assert {row.credits_per_unit for row in copied} == {7}
    # Existing workspaces are not retroactively assigned the template or a cache price.
    listing = owner.get(f"/site/admin/workspaces/{workspace}/usage-policies").json()
    missing = next(row for row in listing if row["capability"] == "llm_cached_input_tokens")
    assert missing["credits_per_unit"] is None and missing["configured"] is False
    assert len(db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()) == 4
    # Site administrator need not belong to the selected workspace.
    assert owner.get(f"/site/admin/workspaces/{target}/usage-policies").status_code == 200
    assert other.get(f"/site/admin/workspaces/{target}/usage-policies").status_code == 403
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: "encrypted")
    created = owner.post(f"/site/admin/workspaces/{target}/llm-providers", json={
        "key": "copy", "name": "Copy", "base_url": "https://example.com/v1", "model": "test", "api_key": "test-secret"})
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]
    provider_prices = owner.get(f"/site/admin/workspaces/{target}/llm-providers/{provider_id}/pricing").json()
    assert len(provider_prices) == 3 and {row["credits_per_unit"] for row in provider_prices} == {7}
    assert {row["template_version"] for row in provider_prices} == {1}
    assert "test-secret" not in str(provider_prices)
    updated = owner.put(f"/site/admin/workspaces/{target}/llm-providers/{provider_id}/pricing/llm_cached_input_tokens",
        json={"capability": "llm_cached_input_tokens", "unit": "tokens", "unit_size": 1000,
              "credits_per_unit": 0, "enabled": True})
    assert updated.status_code == 200 and updated.json()["configured"]
    assert next(row for row in db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == target,
        UsagePolicy.provider_id.is_(None))) if row.capability == "llm_cached_input_tokens").credits_per_unit == 7


def test_template_and_signup_rollback_together(accounts):
    owner, db, _ = administrator(accounts)
    owner.put("/site/admin/pricing-template", json=template())
    from app.auth import create_user
    from app.models import Workspace
    user, workspace = create_user(db, "rollback@example.com", "Rollback")
    assert len(db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()) == 3
    db.rollback()
    assert db.get(Workspace, workspace) is None
    assert db.get(User, user.id) is None
    assert not db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()


def test_runtime_gate_refuses_missing_prices_active_requests_and_pending(accounts):
    owner, db, workspace = administrator(accounts)
    with pytest.raises(ValueError, match="template"):
        prices.switch_billing(db, "enable")
    db.rollback()
    owner.put("/site/admin/pricing-template", json=template())
    with pytest.raises(HTTPException):
        prices.switch_billing(db, "enable")
    db.rollback()
    from app.models import Workspace
    for row in db.scalars(select(Workspace)):
        ensure_policies(db, row.id)
        db.add(UsagePolicy(workspace_id=row.id, capability="llm_cached_input_tokens", unit="tokens",
            unit_size=1000, credits_per_unit=0))
    db.commit()
    assert prices.switch_billing(db, "enable")["billing_enabled"]
    db.commit()
    from uuid import uuid4
    request = ModelUsageRequest(request_id=uuid4(), workspace_id=workspace, status="running", hold=0)
    db.add(request); db.commit()
    with pytest.raises(ValueError, match="Drain"):
        prices.switch_billing(db, "disable")
    request.status = "pending"; request.credits = 1; db.commit()
    with pytest.raises(ValueError, match="pending"):
        prices.switch_billing(db, "disable")
    request.status = "settled"; db.commit()
    assert not prices.switch_billing(db, "disable")["billing_enabled"]
