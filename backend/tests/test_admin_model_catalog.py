from uuid import UUID, uuid4

from sqlalchemy import select

from app.models import LLMProviderConfig, ModelBillingSettings, ModelConnection, SiteModelProvider, UsagePolicy, Workspace
from test_auth import register
from test_shared_providers import setup_site, provider, choose


def test_billing_preview_includes_unused_models_without_writes(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    first = provider(owner, rate=0)
    second = provider(owner, "second", rate=7)
    workspace_id = UUID(session["workspace_id"])
    workspace = db.get(Workspace, workspace_id)
    workspace.billing_multiplier_percent = 125
    db.commit()
    response = owner.get(f"/site/admin/workspaces/{workspace_id}/billing-settings")
    assert response.status_code == 200
    body = response.json()
    assert {p["provider_id"] for p in body["models"]} == {first["id"], second["id"]}
    assert all(p["available"] for p in body["models"])
    assert all(p["configured"] for model in body["models"] for p in model["policies"])
    assert body["models"][0]["policies"][0]["effective_credits_per_unit"] == "0"
    assert body["models"][1]["policies"][0]["effective_credits_per_unit"] == "8.75"
    assert db.scalars(select(LLMProviderConfig)).all() == []
    assert db.scalars(select(UsagePolicy)).all() == []
    assert not db.get(ModelBillingSettings, 1).enabled
    assert owner.get("/auth/me").json()["workspace_id"] == str(workspace_id)


def test_preview_visibility_and_reasons_match_composer(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    ready = provider(owner)
    missing = provider(owner, "missing")
    disabled = provider(owner, "disabled")
    disconnected = provider(owner, "disconnected")
    private = provider(owner, "private")
    choose(owner, private["id"])
    db.get(SiteModelProvider, UUID(private["id"])).published = False
    db.get(SiteModelProvider, UUID(missing["id"])).prices = {}
    db.get(SiteModelProvider, UUID(disabled["id"])).enabled = False
    db.get(ModelConnection, UUID(disconnected["connection_id"])).encrypted_api_key = ""
    db.commit()
    path = f"/site/admin/workspaces/{session['workspace_id']}/billing-settings"
    models = owner.get(path).json()["models"]
    choices = owner.get("/models/available").json()["providers"]
    assert {p["provider_id"]: p["unavailable_reason"] for p in models if p["provider_id"] != disabled["id"]} == {
        p["id"]: p["unavailable_reason"] for p in choices}
    reasons = {p["provider_id"]: p["unavailable_reason"] for p in models}
    assert reasons[ready["id"]] is None
    assert reasons[missing["id"]] == "pricing_unconfigured"
    assert reasons[disabled["id"]] == "model_disabled"
    assert reasons[disconnected["id"]] == "service_not_ready"
    (_, other, _), _ = accounts
    target = register(other, "catalog-other@example.com")["workspace_id"]
    others = owner.get(f"/site/admin/workspaces/{target}/billing-settings").json()["models"]
    assert private["id"] in {p["provider_id"] for p in others}
    assert other.get(path).status_code == 403
    assert len(db.scalars(select(LLMProviderConfig)).all()) == 1


def test_model_detail_permissions_redaction_and_live_diagnostics(accounts, monkeypatch):
    owner, db, _ = setup_site(accounts, monkeypatch)
    model = provider(owner)
    path = f"/site/admin/llm-providers/{model['id']}"
    result = owner.get(path)
    assert result.status_code == 200
    assert result.json()["available"] and result.json()["connection_id"]
    assert "secret" not in result.text and "encrypted_api_key" not in result.text
    db.get(SiteModelProvider, UUID(model["id"])).prices = {}
    db.commit()
    assert owner.get(path).json()["unavailable_reason"] == "pricing_unconfigured"
    assert owner.get("/site/admin/llm-providers").json()[0]["unavailable_reason"] == "pricing_unconfigured"
    assert owner.get(f"/site/admin/llm-providers/{uuid4()}").status_code == 404
    (_, other, anonymous), _ = accounts
    register(other, "detail-member@example.com")
    assert other.get(path).status_code == 403
    assert anonymous.get(path).status_code == 401
