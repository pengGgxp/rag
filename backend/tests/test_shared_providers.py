from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import shared_providers as shared, llm_providers
from app.config import Settings
from app.llm import OpenAICompatibleChatProvider
from app.models import LLMProviderConfig, ModelBillingSettings, SiteModelProvider, UsagePolicy, User, Workspace
from test_auth import accounts, register, share, switch


def prices(rate=4):
    return [{"capability": key, "unit": "tokens", "unit_size": 1000,
        "credits_per_unit": rate, "enabled": True} for key in shared.CAPABILITIES]


def setup_site(accounts, monkeypatch):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    monkeypatch.setattr(shared, "_encrypt_api_key", lambda value: f"enc:{value}")
    monkeypatch.setattr(shared, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    return owner, db, session


def provider(client, key="one", rate=4, published=True):
    result = client.post("/site/admin/llm-providers", json={"key": key, "name": key,
        "model": "model-one", "base_url": "https://provider.example/v1", "api_key": "secret",
        "published": published})
    assert result.status_code == 201, result.text
    row = result.json()
    result = client.put(f"/site/admin/llm-providers/{row['id']}/pricing", json={"version": row["version"], "policies": prices(rate)})
    assert result.status_code == 200, result.text
    row = result.json()
    result = client.put(f"/site/admin/llm-providers/{row['id']}", json={"key": key, "name": key,
        "model": "model-one", "base_url": "https://provider.example/v1", "enabled": True,
        "published": published, "version": row["version"]})
    assert result.status_code == 200, result.text
    return result.json()


def choose(client, identifier):
    selection = client.get("/settings/models").json()
    result = client.put("/settings/models", json={"provider_id": identifier, "version": selection["version"]})
    assert result.status_code == 200, result.text
    return result.json()


def test_shared_once_copy_and_switchback(accounts, monkeypatch):
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
        assert {r.credits_per_unit for r in db.scalars(select(UsagePolicy).where(UsagePolicy.provider_id == a.id))} == {4}
    config = llm_providers.active_provider_config(db, UUID(other_session["workspace_id"]))
    assert config.model == "model-one" and config.api_key == "secret"
    assert config.site_provider_id == p["id"]


def test_permissions_private_visibility_versions_and_secrets(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    p = provider(owner)
    private = provider(owner, "private", published=False)
    (_, other, anonymous), _ = accounts
    register(other, "viewer@example.com")
    assert other.get("/site/admin/llm-providers").status_code == 403
    assert anonymous.get("/settings/models").status_code == 401
    choices = other.get("/settings/models")
    assert "secret" not in choices.text and "base_url" not in choices.text and "prices" not in choices.text
    assert private["id"] not in choices.text
    assert other.put("/settings/models", json={"provider_id": private["id"], "version": 0}).status_code == 404
    share(owner, "viewer@example.com", "viewer")
    switch(other, session["workspace_id"])
    assert other.put("/settings/models", json={"provider_id": p["id"], "version": 0}).status_code == 403
    choose(owner, p["id"])
    assert owner.put("/settings/models", json={"provider_id": p["id"], "version": 0}).status_code == 409
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
    assert len(policies) == 5
    assert other.get("/settings/models").json()["provider_id"] == p["id"]
    assert db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == UUID(first["workspace_id"]))).all() == []
    draft = owner.put("/site/admin/pricing-template", json={"version": 1, "policies": []})
    assert draft.status_code == 200 and not draft.json()["configured"]
    assert db.get(ModelBillingSettings, 1).enabled is False
    assert len(db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace)).all()) == 5


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
    result = owner.post("/site/admin/llm-providers", json={"key": "bad", "name": "Bad", "base_url": "https://a.example/v1",
        "model": "m", "api_key": "key", "enabled": True})
    assert result.status_code == 422
    db.rollback()
    p = provider(owner)
    choose(owner, p["id"])
    row = db.get(SiteModelProvider, UUID(p["id"]))
    row.enabled = False
    db.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        llm_providers.active_provider_config(db, UUID(session["workspace_id"]))
