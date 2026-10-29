from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app import model_connections as connections, llm_providers
from app.models import ModelConnection, SiteModelProvider, LLMProviderConfig, UsageEvent
from test_auth import register
from test_shared_providers import setup_site, prices


def connection(client):
    result = client.post("/site/admin/model-connections", json={"name": "Shared connection",
        "platform": "custom", "base_url": "https://provider.example/v1", "api_key": "secret"})
    assert result.status_code == 201, result.text
    assert "secret" not in result.text and "encrypted_api_key" not in result.text
    return result.json()


def model(c, name="model-a", rate=3):
    return {"name": name, "model": name, "connection_id": c["id"], "connection_version": c["version"],
        "policies": prices(rate), "enabled": True}


def test_shared_connection_independent_prices_and_frozen_request(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    c = connection(client)
    result = client.post(f"/site/admin/model-connections/{c['id']}/models",
        json={"connection_version": 1, "models": [model(c), model(c, "model-b", 8)]})
    assert result.status_code == 201, result.text
    first, second = result.json()
    assert first["prices"] != second["prices"]
    assert first["connection_id"] == second["connection_id"] == c["id"]
    config = llm_providers.request_provider_config(db, UUID(identity["workspace_id"]), UUID(first["id"]))
    db.commit()
    changed = client.put(f"/site/admin/model-connections/{c['id']}", json={
        "name": "Renamed", "base_url": "https://other.example/v1", "api_key": "replacement", "version": 1})
    assert changed.status_code == 200
    assert config.base_url == "https://provider.example/v1" and config.api_key == "secret"
    assert config.connection_version == 1
    from app import shared_providers
    from app.llm import LLMConfigurationError
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=db.get_bind()))
    current = llm_providers.request_provider_config(db, UUID(identity["workspace_id"]), UUID(second["id"]))
    assert current.api_key == "replacement" and current.connection_version == 2
    db.commit()
    assert client.put(f"/site/admin/model-connections/{c['id']}", json={
        "name": "Renamed", "base_url": "https://other.example/v1", "version": 2, "enabled": False}).status_code == 200
    with pytest.raises(LLMConfigurationError):
        shared_providers.assert_available(first["id"], config.connection_id)
    assert all(p["unavailable_reason"] == "connection_disabled" for p in client.get("/models/available").json()["providers"])
    assert client.request("DELETE", f"/site/admin/model-connections/{c['id']}", json={"version": 3}).status_code == 409


def test_batch_atomic_drafts_duplicate_versions_and_permissions(accounts, monkeypatch):
    client, db, _ = setup_site(accounts, monkeypatch)
    c = connection(client)
    path = f"/site/admin/model-connections/{c['id']}/models"
    invalid = {**model(c, "missing-price"), "policies": []}
    result = client.post(path, json={"connection_version": 1, "models": [model(c), invalid]})
    assert result.status_code == 422 and "missing-price" in result.text
    assert not db.scalars(select(SiteModelProvider)).all()
    assert client.post(path, json={"connection_version": 1,
        "models": [model(c), model(c)]}).status_code == 422
    assert client.post(path, json={"connection_version": 2, "models": [model(c)]}).status_code == 409
    draft = client.post(path, json={"connection_version": 1, "models": [{**invalid, "enabled": False}]})
    assert draft.status_code == 201 and not draft.json()[0]["available"]
    assert client.post(path, json={"connection_version": 1,
        "models": [{**invalid, "enabled": False}]}).status_code == 422
    (_, member, anonymous), _ = accounts
    register(member, "connection-viewer@example.com")
    for route in ("/site/admin/model-connections", f"/site/admin/model-connections/{c['id']}"):
        assert member.get(route).status_code == 403
        assert anonymous.get(route).status_code == 401
    assert member.post(path, json={"connection_version": 1, "models": [model(c)]}).status_code == 403
    assert "secret" not in member.get("/models/available").text


def test_discovery_is_bounded_readonly_and_does_not_follow_redirects(accounts, monkeypatch):
    client, db, _ = setup_site(accounts, monkeypatch)
    c = connection(client)
    seen = []
    @contextmanager
    def upstream(method, url, **kwargs):
        seen.append((method, url, kwargs))
        yield httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"},
            {"id": None}, {"name": "ignore"}]})
    monkeypatch.setattr(httpx, "stream", upstream)
    path = f"/site/admin/model-connections/{c['id']}/models/discover"
    result = client.post(path, json={"version": 1})
    assert result.status_code == 200 and result.json()["models"] == ["model-a", "model-b"]
    assert seen[0][0:2] == ("GET", "https://provider.example/v1/models")
    assert seen[0][2]["follow_redirects"] is False
    assert not db.scalars(select(SiteModelProvider)).all()
    assert not db.scalars(select(LLMProviderConfig)).all()
    assert not db.scalars(select(UsageEvent)).all()
    @contextmanager
    def redirect(*args, **kwargs):
        yield httpx.Response(302, headers={"location": "https://secret.example"})
    monkeypatch.setattr(httpx, "stream", redirect)
    response = client.post(path, json={"version": 1})
    assert response.status_code == 502 and "secret.example" not in response.text
    @contextmanager
    def oversized(*args, **kwargs):
        yield httpx.Response(200, content=b" " * (1024 * 1024 + 1))
    monkeypatch.setattr(httpx, "stream", oversized)
    assert client.post(path, json={"version": 1}).status_code == 502
    @contextmanager
    def empty(*args, **kwargs):
        yield httpx.Response(200, json={"data": []})
    monkeypatch.setattr(httpx, "stream", empty)
    assert client.post(path, json={"version": 1}).json()["models"] == []
    ticks = iter((0, 11))
    monkeypatch.setattr(connections, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(httpx, "stream", upstream)
    assert client.post(path, json={"version": 1}).status_code == 502


def test_same_platform_connections_keep_keys_and_accept_explicit_zero(accounts, monkeypatch):
    client, db, _ = setup_site(accounts, monkeypatch)
    first, second = connection(client), connection(client)
    assert first["id"] != second["id"]
    result = client.post(f"/site/admin/model-connections/{first['id']}/models",
        json={"connection_version": 1, "models": [model(first, rate=0)]})
    assert result.status_code == 201 and result.json()[0]["available"]
    result2 = client.post(f"/site/admin/model-connections/{second['id']}/models",
        json={"connection_version": 1, "models": [model(second)]})
    assert result2.status_code == 201
    before = db.get(ModelConnection, UUID(first["id"])).encrypted_api_key
    saved = client.put(f"/site/admin/model-connections/{first['id']}", json={
        "name": "Updated without key", "base_url": first["base_url"], "version": 1})
    assert saved.status_code == 200 and saved.json()["has_api_key"]
    assert db.get(ModelConnection, UUID(first["id"])).encrypted_api_key == before
    assert client.put(f"/site/admin/model-connections/{first['id']}", json={
        "name": "Stale", "base_url": first["base_url"], "version": 1}).status_code == 409
