from uuid import UUID
import pytest
from sqlalchemy import select
from app import model_pricing as prices
from app.models import ModelBillingSettings, SiteAuditEvent, UsagePolicy, User
from test_auth import accounts, register
from test_workspace_billing import shared_billing


def template(version=0, rate=4):
    return {"version": version, "policies": [{"capability": key,
        "unit": "pages" if key == "ocr_pages" else "tokens", "unit_size": 1 if key == "ocr_pages" else 1000,
        "credits_per_unit": rate, "enabled": True} for key in prices.TEMPLATE_CAPABILITIES]}


def administrator(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    return owner, db, UUID(session["user"]["id"])


def test_template_permissions_completeness_zero_and_version(accounts):
    (owner, _, anonymous), db = accounts
    session = register(owner)
    assert owner.get("/site/admin/pricing-template").status_code == 403
    assert owner.put("/site/admin/pricing-template", json=template()).status_code == 403
    assert anonymous.get("/site/admin/pricing-template").status_code == 401
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    empty = owner.get("/site/admin/pricing-template").json()
    assert not empty["configured"] and not empty["billing_enabled"] and len(empty["policies"]) == 5
    invalid = template(); invalid["policies"][1] = invalid["policies"][0]
    assert owner.put("/site/admin/pricing-template", json=invalid).status_code == 422
    assert owner.put("/site/admin/pricing-template", json=template(rate=0)).json()["configured"]
    assert owner.put("/site/admin/pricing-template", json=template()).status_code == 409
    assert db.scalar(select(SiteAuditEvent).where(SiteAuditEvent.action == "billing.template_updated"))


@pytest.mark.parametrize("value", [True, -1, 0.00001, None, "invalid"])
def test_template_rejects_unreliable_prices(accounts, value):
    owner, _, _ = administrator(accounts)
    payload = template(); payload["policies"][1]["credits_per_unit"] = value
    assert owner.put("/site/admin/pricing-template", json=payload).status_code == 422


def test_document_template_is_live_and_never_copies_workspace_policies(accounts):
    owner, db, original = administrator(accounts)
    assert owner.put("/site/admin/pricing-template", json=template(rate=7)).status_code == 200
    (_, other, _), _ = accounts
    workspace = UUID(register(other, "template@example.com")["user"]["id"])
    copied = db.scalars(select(UsagePolicy).where(UsagePolicy.account_id == workspace)).all()
    assert copied == []
    owner.put("/site/admin/pricing-template", json=template(version=1, rate=8))
    db.expire_all()
    assert db.get(ModelBillingSettings, 1).template["ocr_pages"]["credits_per_unit"] == 8
    assert owner.get(f"/site/admin/workspaces/{original}/usage-policies").status_code == 404
    assert db.scalars(select(UsagePolicy).where(UsagePolicy.account_id == original)).all() == []


def test_template_and_signup_rollback_together(accounts):
    owner, db, _ = administrator(accounts)
    owner.put("/site/admin/pricing-template", json=template())
    from app.auth import create_user
    user = create_user(db, "rollback@example.com", "Rollback")
    workspace = user.id
    assert db.scalars(select(UsagePolicy).where(UsagePolicy.account_id == workspace)).all() == []
    db.rollback()
    assert db.get(User, workspace) is None
    assert not db.scalars(select(UsagePolicy).where(UsagePolicy.account_id == workspace)).all()


def test_draft_does_not_enable_billing_and_units_are_checked(accounts):
    owner, db, _ = administrator(accounts)
    draft = template(); draft["policies"] = draft["policies"][:1]
    assert owner.put("/site/admin/pricing-template", json=draft).status_code == 200
    assert not db.get(ModelBillingSettings, 1).enabled
    bad = template(version=1); bad["policies"][-1]["unit"] = "pages"
    assert owner.put("/site/admin/pricing-template", json=bad).status_code == 422


def test_enable_shared_billing_checks_models_not_workspace_active_or_template(shared_billing):
    from app.models import LLMProviderConfig
    _, db, _, _, _ = shared_billing
    for binding in db.scalars(select(LLMProviderConfig)):
        binding.active = False
    settings = db.get(ModelBillingSettings, 1)
    settings.template = {}
    db.commit()
    before = db.scalar(select(UsagePolicy.id).limit(1))
    assert prices.switch_billing(db, "enable")["billing_enabled"]
    db.commit()
    assert db.scalar(select(UsagePolicy.id).limit(1)) == before


def test_enable_validates_unassociated_model_and_allows_explicit_zero(shared_billing):
    from app.models import SiteModelProvider
    _, db, _, _, identifier = shared_billing
    existing = db.get(SiteModelProvider, identifier)
    other = SiteModelProvider(key="unassociated", name="Unassociated", model="other",
        connection_id=existing.connection_id, enabled=True, prices={})
    db.add(other); db.commit()
    with pytest.raises(ValueError, match="Unassociated"):
        prices.switch_billing(db, "enable")
    db.rollback()
    assert not db.get(ModelBillingSettings, 1).enabled
    other.prices = {key: {"capability": key, "unit": "tokens", "unit_size": 1000000,
        "credits_per_unit": 0, "enabled": True} for key in prices.CAPABILITIES}
    db.commit()
    assert prices.switch_billing(db, "enable")["billing_enabled"]


def test_switch_rejects_running_manual_compaction(shared_billing):
    from uuid import uuid4
    from app.models import ContextOperation
    client, db, _, _, _ = shared_billing
    conversation = client.post("/conversations", json={}).json()
    db.add(ContextOperation(request_id=uuid4(), conversation_id=UUID(conversation["id"]), status="running"))
    db.commit()
    with pytest.raises(ValueError, match="Drain"):
        prices.switch_billing(db, "enable")
