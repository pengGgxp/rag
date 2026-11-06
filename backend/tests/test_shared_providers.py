from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import shared_providers as shared, llm_providers
from app.config import Settings
from app.llm import OpenAICompatibleChatProvider
from app.models import LLMProviderConfig, ModelBillingSettings, ModelConnection, SiteModelProvider, UsagePolicy, User, Workspace
from test_auth import accounts, register, share, switch


def prices(rate=4):
    return [{"capability": key, "unit": "tokens", "unit_size": 1000,
        "credits_per_unit": rate, "enabled": True} for key in shared.CAPABILITIES]


def setup_site(accounts, monkeypatch):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.add(ModelBillingSettings(id=1, shared_pricing_enabled=True, enabled=False, template={}, version=0))
    db.commit()
    monkeypatch.setattr(shared, "_encrypt_api_key", lambda value: f"enc:{value}")
    monkeypatch.setattr(shared, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    from app import model_connections
    monkeypatch.setattr(model_connections, "_encrypt_api_key", lambda value: f"enc:{value}")
    monkeypatch.setattr(model_connections, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    return owner, db, session


def provider(client, key="one", rate=4):
    connection = client.post("/site/admin/model-connections", json={"name": key,
        "api_protocol": "chat_completions", "base_url": "https://provider.example/v1", "api_key": "secret"}).json()
    result = client.post("/site/admin/llm-providers", json={"name": key,
        "model": "model-one", "connection_id": connection["id"], "connection_version": connection["version"]})
    assert result.status_code == 201, result.text
    row = result.json()
    result = client.put(f"/site/admin/llm-providers/{row['id']}/pricing", json={"version": row["version"], "policies": prices(rate)})
    assert result.status_code == 200, result.text
    row = result.json()
    result = client.put(f"/site/admin/llm-providers/{row['id']}", json={"name": key,
        "model": "model-one", "connection_id": connection["id"], "connection_version": connection["version"], "enabled": True,
        "version": row["version"]})
    assert result.status_code == 200, result.text
    return result.json()


def choose(client, identifier):
    from app.main import app, get_db
    db = app.dependency_overrides[get_db]()
    workspace_id = UUID(client.get("/auth/me").json()["workspace_id"])
    binding, _ = shared.request_binding(db, workspace_id, UUID(identifier))
    db.commit()
    return binding


def test_shared_selection_has_no_copies_and_switchback_uses_live_prices(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    p = provider(owner)
    q = provider(owner, "two", 8)
    (_, other, _), _ = accounts
    other_session = register(other, "other@example.com")
    choose(owner, p["id"])
    choose(other, p["id"])
    associations = db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.site_provider_id == UUID(p["id"]))).all()
    assert len(associations) == 2
    assert all(not a.encrypted_api_key for a in associations)
    updated = owner.put(f"/site/admin/llm-providers/{p['id']}/pricing", json={"version": p["version"], "policies": prices(20)})
    assert updated.status_code == 200
    choose(owner, q["id"])
    choose(owner, p["id"])
    assert len(db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == UUID(session["workspace_id"]))).all()) == 2
    for a in associations:
        assert db.scalars(select(UsagePolicy).where(UsagePolicy.provider_id == a.id)).all() == []
    from app.model_billing import price_snapshot
    assert price_snapshot(db, associations[0].workspace_id, associations[0].id)["llm_input_tokens"]["credits_per_unit"] == 20
    config = llm_providers.request_provider_config(db, UUID(other_session["workspace_id"]), UUID(p["id"]))
    assert config.model == "model-one" and config.api_key == "secret"
    assert config.site_provider_id == p["id"]


def test_permissions_universal_visibility_versions_and_secrets(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    p = provider(owner)
    private = provider(owner, "legacy")
    db.get(SiteModelProvider, UUID(private["id"])).published = False
    db.commit()
    (_, other, anonymous), _ = accounts
    register(other, "viewer@example.com")
    assert other.get("/site/admin/llm-providers").status_code == 403
    assert anonymous.get("/models/available").status_code == 401
    choices = other.get("/models/available")
    assert "secret" not in choices.text and "base_url" not in choices.text and "prices" not in choices.text
    assert private["id"] in choices.text
    assert "published" not in owner.get(f"/site/admin/llm-providers/{private['id']}").text
    assert owner.post(f"/site/admin/llm-providers/{private['id']}/default",
        json={"version": private["version"]}).status_code == 200
    shared.request_binding(db, UUID(other.get("/auth/me").json()["workspace_id"]), UUID(private["id"]))
    db.rollback()
    share(owner, "viewer@example.com", "viewer")
    switch(other, session["workspace_id"])
    assert other.put("/settings/models", json={"provider_id": p["id"], "version": 0}).status_code == 403
    choose(owner, p["id"])
    assert owner.put("/settings/models", json={"provider_id": p["id"], "version": 0}).status_code == 410
    assert owner.post("/settings/llm-providers", json={}).status_code == 410
    assert owner.request("DELETE", f"/site/admin/llm-providers/{p['id']}", json={"version": p["version"]}).status_code == 409


def test_template_documents_drafts_default_and_no_backfill(accounts, monkeypatch):
    owner, db, first = setup_site(accounts, monkeypatch)
    body = {"version": 0, "policies": prices(0) + [
        {"capability": "ocr_pages", "unit": "pages", "unit_size": 1, "credits_per_unit": 11, "enabled": True},
        {"capability": "embedding_tokens", "unit": "tokens", "unit_size": 1000, "credits_per_unit": 2, "enabled": True}]}
    assert owner.put("/site/admin/pricing-template", json=body).json()["configured"]
    p = provider(owner)
    assert owner.post(f"/site/admin/llm-providers/{p['id']}/default", json={"version": p["version"]}).status_code == 200
    (_, other, _), _ = accounts
    registered = register(other, "new@example.com")
    workspace = UUID(registered["workspace_id"])
    policies = db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()
    assert len(policies) == 0
    assert other.get("/models/available").json()["default_provider_id"] == p["id"]
    assert not db.scalar(select(LLMProviderConfig.id).where(LLMProviderConfig.workspace_id == workspace))
    assert db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == UUID(first["workspace_id"]))).all() == []
    draft = owner.put("/site/admin/pricing-template", json={"version": 1, "policies": []})
    assert draft.status_code == 200 and not draft.json()["configured"]
    assert db.get(ModelBillingSettings, 1).enabled is False
    assert len(db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()) == 0


def test_migration_idempotent_preserves_ids_and_partial_prices(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    workspace = UUID(session["workspace_id"])
    old = LLMProviderConfig(workspace_id=workspace, key="old", name="Old", model="old-model",
        base_url="https://old.example/v1", encrypted_api_key="enc:key", active=True, enabled=True)
    db.add(old)
    db.flush()
    old_id = old.id
    policy = UsagePolicy(workspace_id=workspace, provider_id=old.id, capability="llm_input_tokens",
        unit="tokens", unit_size=1000, credits_per_unit=19, version=6)
    db.add(policy)
    db.add(Workspace(name="Environment workspace"))
    db.commit()
    settings = Settings(llm_base_url="https://env.example/v1", llm_api_key=SecretStr("env-secret"), llm_model="env-model")
    assert shared.migrate_connections(db, settings) == 2
    db.commit()
    assert shared.migrate_connections(db, settings) == 0
    db.commit()
    later = Workspace(name="Not an environment migration target")
    db.add(later)
    db.commit()
    assert shared.migrate_connections(db, settings) == 0
    assert not db.scalar(select(LLMProviderConfig.id).where(LLMProviderConfig.workspace_id == later.id))
    assert db.get(LLMProviderConfig, old_id).site_provider_id
    assert not db.get(LLMProviderConfig, old_id).encrypted_api_key
    assert db.get(SiteModelProvider, old.site_provider_id).published is False
    assert policy.credits_per_unit == 19 and policy.version == 6
    assert "llm_cached_input_tokens" not in db.get(SiteModelProvider, old.site_provider_id).prices
    assert not OpenAICompatibleChatProvider(settings=settings).configured


def test_incomplete_enable_and_disabled_resolution(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    c = owner.post("/site/admin/model-connections", json={"name": "Bad", "api_protocol": "chat_completions", "base_url": "https://a.example/v1",
        "api_key": "key"}).json()
    result = owner.post("/site/admin/llm-providers", json={"name": "Bad",
        "connection_id": c["id"], "connection_version": c["version"], "model": "m", "enabled": True})
    assert result.status_code == 422
    db.rollback()
    p = provider(owner)
    choose(owner, p["id"])
    row = db.get(SiteModelProvider, UUID(p["id"]))
    row.enabled = False
    db.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        llm_providers.request_provider_config(db, UUID(session["workspace_id"]), UUID(p["id"]))


@pytest.mark.parametrize("mode,reason", [
    ("ready", None), ("zero", None), ("missing", "pricing_unconfigured"),
    ("disabled", "model_disabled"), ("connection", "service_not_ready"),
    ("legacy_new", None), ("image_only", "not_chat_model"),
])
def test_choice_and_save_share_availability(accounts, monkeypatch, mode, reason):
    owner, db, session = setup_site(accounts, monkeypatch)
    p = provider(owner, rate=0 if mode == "zero" else 4)
    row = db.get(SiteModelProvider, UUID(p["id"]))
    if mode == "missing":
        row.prices = {}
    elif mode == "image_only":
        row.supports_chat = False
    elif mode == "disabled":
        row.enabled = False
    elif mode == "connection":
        db.get(ModelConnection, row.connection_id).encrypted_api_key = ""
    elif mode == "legacy_new":
        db.get(ModelBillingSettings, 1).shared_pricing_enabled = False
    db.commit()
    result = owner.get("/models/available").json()
    if mode in ("disabled", "image_only"):
        assert not any(item["id"] == p["id"] for item in result["providers"])
        assert owner.get(f"/site/admin/llm-providers/{p['id']}").status_code == 200
    else:
        choice = next(item for item in result["providers"] if item["id"] == p["id"])
        assert choice["unavailable_reason"] == reason
        assert choice["available"] is (reason is None)
    if reason:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            shared.request_binding(db, UUID(session["workspace_id"]), UUID(p["id"]))
        assert exc.value.status_code == 409
        db.rollback()
    else:
        choose(owner, p["id"])
    assert db.get(ModelBillingSettings, 1).enabled is False


def test_legacy_binding_missing_prices_cannot_be_selected(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    p = provider(owner)
    choose(owner, p["id"])
    db.get(ModelBillingSettings, 1).shared_pricing_enabled = False
    db.commit()
    result = owner.get("/models/available").json()
    assert result["providers"][0]["unavailable_reason"] is None
    assert owner.put("/settings/models", json={"provider_id": p["id"],
        "version": 0}).status_code == 410
