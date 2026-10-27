from uuid import UUID, uuid4
from sqlalchemy import select
from app import shared_providers, llm_providers
from app.models import SiteAuditEvent, UsageEvent, LLMProviderConfig
from app.credits import authorization_hold, settle_model_usage
from test_auth import register, share, switch
from test_site_admin import setup
from test_shared_providers import provider


def test_management_requires_site_role_on_every_new_and_legacy_route(accounts):
    admin, owner, anonymous, db, a, b = setup(accounts)
    identifier = uuid4()
    for prefix in (f"/site/admin/workspaces/{b['workspace_id']}/llm-providers", "/settings/llm-providers", "/site/admin/llm-providers"):
        for method, suffix in (("GET", ""), ("POST", ""), ("PUT", f"/{identifier}"), ("DELETE", f"/{identifier}")):
            assert owner.request(method, prefix + suffix, json={}).status_code == 403
            assert anonymous.request(method, prefix + suffix, json={}).status_code == 401
    for prefix in (f"/site/admin/workspaces/{b['workspace_id']}/usage-policies", "/usage/policies"):
        assert owner.get(prefix).status_code == 403


def test_admin_can_manage_unjoined_workspace_without_changing_session(accounts, monkeypatch):
    admin, owner, _, db, a, b = setup(accounts)
    monkeypatch.setattr(shared_providers, "_encrypt_api_key", lambda value: "enc:" + value)
    p = provider(admin)
    base = f"/site/admin/workspaces/{b['workspace_id']}"
    result = admin.put(base + "/model", json={"provider_id": p["id"], "version": 0})
    assert result.status_code == 200, result.text
    assert admin.get("/auth/me").json()["workspace_id"] == a["workspace_id"]
    binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == UUID(b["workspace_id"])))
    own = f"/site/admin/workspaces/{a['workspace_id']}"
    assert admin.get(own + f"/llm-providers/{binding.id}/pricing").status_code == 404
    assert admin.post(base + "/llm-providers", json={}).status_code == 410
    assert owner.put(base + "/model", json={"provider_id": p["id"], "version": 1}).status_code == 403
    assert admin.get(f"/site/admin/workspaces/{uuid4()}/model").status_code == 404


def test_updates_preserve_credentials_and_historical_usage(accounts, monkeypatch):
    admin, owner, _, db, a, b = setup(accounts)
    monkeypatch.setattr(shared_providers, "_encrypt_api_key", lambda value: "enc:" + value)
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    p = provider(admin)
    base = f"/site/admin/workspaces/{b['workspace_id']}"
    admin.put(base + "/model", json={"provider_id": p["id"], "version": 0})
    binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == UUID(b["workspace_id"])))
    request_id = uuid4()
    authorization_hold(db, binding.workspace_id, UUID(b["user"]["id"]), request_id)
    settle_model_usage(db, binding.workspace_id, UUID(b["user"]["id"]), request_id, [{
        "prompt_tokens": 1000, "completion_tokens": 0, "provider_id": str(binding.id), "provider_key": binding.key}])
    db.commit()
    old = db.scalar(select(UsageEvent).where(UsageEvent.request_id == request_id, UsageEvent.capability == "llm_input_tokens"))
    snapshot = dict(old.policy_snapshot)
    path = base + f"/llm-providers/{binding.id}/pricing/llm_input_tokens"
    payload = {"capability": "llm_input_tokens", "unit": "tokens", "unit_size": 1000, "credits_per_unit": 9, "enabled": True, "version": 1}
    assert admin.put(path, json=payload).status_code == 200
    assert admin.put(path, json=payload).status_code == 409
    db.refresh(old)
    assert old.credits == 4 and old.policy_snapshot == snapshot
    assert llm_providers.active_provider_config(db, binding.workspace_id).api_key == "secret"
    assert "enc:secret" not in admin.get(base + "/llm-providers").text
    failures = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.result == "failure")).all()
    assert failures and "secret" not in str([e.details for e in failures])


def test_ordinary_member_cannot_use_management_apis(accounts):
    admin, owner, member, db, _, b = setup(accounts)
    user = register(member, "ordinary-viewer@example.com")
    share(owner, user["user"]["email"], "viewer")
    switch(member, b["workspace_id"])
    for path in ("/usage/policies", "/settings/llm-providers", "/site/admin/llm-providers"):
        assert member.get(path).status_code == 403
    assert member.get("/settings/models").status_code == 200
    assert member.put("/settings/models", json={"provider_id": str(uuid4()), "version": 0}).status_code == 403
