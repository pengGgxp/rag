from uuid import UUID, uuid4

from sqlalchemy import select

from app import llm_providers
from app.models import SiteAuditEvent, User, UsageEvent
from app.credits import authorization_hold, settle_model_usage
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
    assert len(prices) == 3
    missing_cache = next(row for row in prices if row["capability"] == "llm_cached_input_tokens")
    assert missing_cache["configured"] is False and missing_cache["credits_per_unit"] is None
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
    assert all(e.details["workspace_id"] == b["workspace_id"] for e in events if e.result == "success")
    assert "never-show-this-secret" not in str([e.details for e in events])
    assert next(p for p in before if p["capability"] == "ocr_pages")["credits_per_unit"] == 10
    assert admin.get(f"/site/admin/workspaces/{uuid4()}/usage-policies").status_code == 404
    assert admin.get("/site/admin/workspaces/invalid/usage-policies").status_code == 422


def test_updates_preserve_credentials_and_historical_usage(accounts, monkeypatch):
    admin, owner, _, db, a, b = setup(accounts)
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: "encrypted:" + value)
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("encrypted:"))
    base = f"/site/admin/workspaces/{b['workspace_id']}"
    created = admin.post(base + "/llm-providers", json=PROVIDER).json()
    provider_id = created["id"]
    admin.get(base + f"/llm-providers/{provider_id}/pricing")
    admin.put(base + f"/llm-providers/{provider_id}/pricing/llm_input_tokens", json=PRICE)
    request_id = uuid4()
    authorization_hold(db, UUID(b["workspace_id"]), UUID(b["user"]["id"]), request_id)
    settle_model_usage(db, UUID(b["workspace_id"]), UUID(b["user"]["id"]), request_id, [{
        "prompt_tokens": 1000, "completion_tokens": 0, "provider_id": provider_id, "provider_key": "test",
    }])
    db.commit()
    old = db.scalar(select(UsageEvent).where(UsageEvent.request_id == request_id, UsageEvent.capability == "llm_input_tokens"))
    old_snapshot = dict(old.policy_snapshot)
    changed = {**PRICE, "credits_per_unit": 9}
    response = admin.put(base + f"/llm-providers/{provider_id}/pricing/llm_input_tokens", json=changed)
    assert response.status_code == 200 and response.json()["version"] == 3
    db.refresh(old)
    assert old.policy_snapshot == old_snapshot and old.credits == 4
    fields = {k: v for k, v in PROVIDER.items() if k != "api_key"}
    fields["name"] = "Updated"
    assert admin.put(base + f"/llm-providers/{provider_id}", json=fields).status_code == 200
    assert llm_providers.active_provider_config(db, UUID(b["workspace_id"])).api_key == PROVIDER["api_key"]
    assert "never-show-this-secret" not in admin.get(base + "/llm-providers").text
    bad = admin.put(base + f"/llm-providers/{provider_id}/pricing/llm_output_tokens", json=PRICE)
    assert bad.status_code == 422
    failed = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.result == "failure")).all()
    assert any(event.action == "workspace.pricing_management_failed" for event in failed)
    assert all("never-show-this-secret" not in str(event.details) for event in failed)


def test_ordinary_member_cannot_use_management_apis(accounts):
    admin, owner, member, db, _, b = setup(accounts)
    user = register(member, "ordinary-viewer@example.com")
    share(owner, user["user"]["email"], "viewer")
    switch(member, b["workspace_id"])
    for path in ("/usage/policies", "/settings/llm-providers", f"/site/admin/workspaces/{b['workspace_id']}/usage-policies", f"/site/admin/workspaces/{b['workspace_id']}/llm-providers"):
        assert member.get(path).status_code == 403
    assert member.get("/usage").status_code == 200
