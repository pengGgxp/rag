from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import deployment, main
from app.db import Base


TOKEN = "test-deployment-control-token-with-at-least-32-characters"


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = deployment.get_settings().model_copy(
        update={"deploy_control_token": SecretStr(TOKEN)}
    )
    monkeypatch.setattr(deployment, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deployment,
        "_loopback",
        lambda host: host in {"testclient", "127.0.0.1"},
    )
    main.app.dependency_overrides[main.get_db] = lambda: db
    client = TestClient(main.app)
    try:
        yield client, db
    finally:
        client.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        db.close()
        engine.dispose()


def headers():
    return {"Authorization": f"Bearer {TOKEN}", "X-RAG-Request": "1"}


def test_maintenance_control_requires_loopback_and_token(api):
    client, _ = api
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/internal/deploy/maintenance",
            "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
            "client": ("203.0.113.10", 50000),
        }
    )
    with pytest.raises(deployment.HTTPException) as rejected:
        deployment.require_control(request)
    assert rejected.value.status_code == 404
    assert client.get("/internal/deploy/maintenance").status_code == 401
    assert client.get(
        "/internal/deploy/maintenance",
        headers={"Authorization": "Bearer wrong" * 2},
    ).status_code == 401


def test_maintenance_state_round_trip(api):
    client, _ = api
    response = client.put(
        "/internal/deploy/maintenance",
        headers=headers(),
        json={"active": True, "release": "v1.2.3", "message": "deploying"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is True
    assert response.json()["version"] == 2
    status = client.get("/internal/deploy/maintenance", headers=headers())
    assert status.json()["release"] == "v1.2.3"


def test_maintenance_middleware_blocks_new_writes_but_allows_stop(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(deployment, "is_active", lambda: True)
    blocked = client.post("/knowledge-bases", json={"name": "blocked"})
    assert blocked.status_code == 503
    assert blocked.headers["retry-after"] == "60"
    callback = client.post("/payments/zhifux/notify", content=b"")
    assert callback.status_code != 503
    stop = client.post(
        "/conversations/00000000-0000-0000-0000-000000000000/stop",
        json={},
    )
    assert stop.status_code != 503


def test_maintenance_middleware_allows_read_health_and_payment_callback(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(deployment, "is_active", lambda: True)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in {200, 503}
    callback = client.post("/payments/ezfp/notify", content=b"")
    assert callback.status_code != 503
    order_status = client.post(
        "/payments/orders/00000000-0000-0000-0000-000000000000",
        content=b"",
    )
    assert order_status.status_code != 503
    assert client.post("/payments/orders", json={}).status_code == 503


def test_version_reports_release_identity(monkeypatch):
    settings = deployment.get_settings().model_copy(
        update={
            "app_version": "v1.2.3",
            "git_commit": "a" * 40,
            "config_contract": 1,
            "deployment_rollback": "automatic-app-rollback",
        }
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    client = TestClient(main.app)
    try:
        response = client.get("/version")
    finally:
        client.close()
    assert response.status_code == 200
    assert response.json() == {
        "version": "v1.2.3",
        "commit": "a" * 40,
        "config_contract": 1,
        "database_rollback": "automatic-app-rollback",
    }
