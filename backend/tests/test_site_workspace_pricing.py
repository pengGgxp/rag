from uuid import UUID, uuid4

from sqlalchemy import select

from app import llm_providers
from app.models import SiteAuditEvent, User, UsageEvent
from test_auth import register, share, switch
from test_site_admin import setup


PRICE = {"capability": "llm_input_tokens", "unit": "tokens", "unit_size": 250,
         "credits_per_unit": 1, "enabled": True}
PROVIDER = {"key": "test", "name": "Test", "base_url": "https://example.com/v1",
            "model": "test-model", "api_key": "never-show-this-secret"}


def test_management_requires_site_role_on_every_new_and_legacy_route(accounts):
    admin, owner, anonymous, db, a, b = setup(accounts)
    base = f"/site/admin/workspaces/{b['workspace_id']}"
    identifier = uuid4()
    for prefix in (base + "/llm-providers", "/settings/llm-providers"):
        for method, path, body in (
            ("GET", "", None), ("GET", "/current", None), ("POST", "", PROVIDER),
            ("PUT", f"/{identifier}", PROVIDER), ("DELETE", f"/{identifier}", None),
            ("POST", f"/{identifier}/activate", None), ("GET", f"/{identifier}/pricing", None),
            ("PUT", f"/{identifier}/pricing/llm_input_tokens", PRICE),
        ):
            assert owner.request(method, prefix + path, json=body).status_code == 403
            assert anonymous.request(method, prefix + path, json=body).status_code == 401
    for prefix in (base + "/usage-policies", "/usage/policies"):
        assert owner.get(prefix).status_code == 403
        assert owner.put(prefix + "/llm_input_tokens", json=PRICE).status_code == 403
    share(owner, a["user"]["email"], "viewer")
    switch(admin, b["workspace_id"])
    # Site authority does not depend on the workspace role.
    assert admin.get("/usage/policies").status_code == 200
    db.get(User, UUID(a["user"]["id"])).is_site_admin = False
    db.commit()
    assert admin.get(base + "/usage-policies").status_code == 403
    assert admin.get("/usage/policies").status_code == 403


def test_admin_can_manage_unjoined_workspace_without_changing_session(accounts, monkeypatch):
    admin, owner, _, db, a, b = setup(accounts)
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: "encrypted:" + value)
    base = f"/site/admin/workspaces/{b['workspace_id']}"
    own = f"/site/admin/workspaces/{a['workspace_id']}"
    before = admin.get(base + "/usage-policies").json()
    assert admin.get(own + "/usage-policies").status_code == 200
    created = admin.post(base + "/llm-providers", json=PROVIDER)
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    assert "never-show-this-secret" not in created.text
    assert admin.get(own + "/llm-providers").json() == []
    assert admin.put(own + f"/llm-providers/{identifier}", json=PROVIDER).status_code == 404
    assert admin.get(own + f"/llm-providers/{identifier}/pricing").status_code == 404
    assert admin.post(own + f"/llm-providers/{identifier}/activate").status_code == 404
    assert admin.delete(own + f"/llm-providers/{identifier}").status_code == 404
    prices = admin.get(base + f"/llm-providers/{identifier}/pricing").json()
    assert len(prices) == 2
    assert admin.put(base + f"/llm-providers/{identifier}/pricing/llm_input_tokens", json=PRICE).json()["version"] == 2
    updated = admin.put(base + "/usage-policies/llm_input_tokens", json=PRICE)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert admin.get("/auth/me").json()["workspace_id"] == a["workspace_id"]
    own_prices = admin.get("/usage/policies?workspace_id=" + b["workspace_id"]).json()
    assert next(p for p in own_prices if p["capability"] == "llm_input_tokens")["unit_size"] == 1000
    assert len(db.scalars(select(UsageEvent)).all()) == 0
    assert owner.get("/usage").json()["balance"] == 1000
    events = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.action.like("workspace.%"))).all()
    assert {e.action for e in events} >= {"workspace.model_provider_created", "workspace.model_pricing_updated", "workspace.usage_policy_updated"}
    assert all(e.details["workspace_id"] == b["workspace_id"] for e in events)
    assert "never-show-this-secret" not in str([e.details for e in events])
    assert next(p for p in before if p["capability"] == "ocr_pages")["credits_per_unit"] == 10
    assert admin.get(f"/site/admin/workspaces/{uuid4()}/usage-policies").status_code == 404
    assert admin.get("/site/admin/workspaces/invalid/usage-policies").status_code == 422
