import io
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.images import tasks as image_tasks, adapters as image_adapters
from app.images.adapters import validate_image, ImageUncertain
from app.models import UserCreditAccount, CreditLedgerEntry, ImageTask, SiteModelProvider
from test_shared_providers import setup_site
from test_model_connections import connection, model


def test_image_api_and_worker_contract_is_stable():
    from app.main import app
    from app.tasks import process_image_task, recover_image_tasks

    expected_routes = {
        "/image-tasks": {"post"},
        "/image-tasks/by-request/{key}": {"get"},
        "/image-tasks/{identifier}": {"get"},
        "/image-tasks/{identifier}/cancel": {"post"},
        "/image-tasks/{identifier}/retry-save": {"post"},
        "/site/admin/image-models": {"get", "put"},
        "/site/admin/image-models/tasks": {"get"},
        "/site/admin/image-models/tasks/{identifier}/resolve": {"post"},
    }
    paths = app.openapi()["paths"]
    actual_routes = {
        path: {method for method in operations if method in {"get", "post", "put", "patch", "delete"}}
        for path, operations in paths.items()
        if path.startswith("/image-tasks") or path.startswith("/site/admin/image-models")
    }
    assert actual_routes == expected_routes
    assert process_image_task.name == "app.tasks.process_image_task"
    assert recover_image_tasks.name == "app.tasks.recover_image_tasks"


def configure(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    c = connection(client)
    config = {"generate": True, "edit": True, "specs": [{"operation": op, "size": "1024x1024", "quality": "standard", "credits_per_image": 3.25} for op in ("generate", "edit")]}
    result = client.post(f"/site/admin/model-connections/{c['id']}/models", json={"connection_version": 1,
        "models": [{**model(c), "supports_chat": False, "policies": [], "image_config": config}]})
    assert result.status_code == 201, result.text
    identifier = result.json()[0]["id"]
    saved = client.put("/site/admin/image-models", json={"version": 0, "generate_model_id": identifier, "edit_model_id": identifier})
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(image_tasks, "dispatch", lambda identifier: None)
    from app.model_catalog import providers as llm_providers
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: f"enc:{value}")
    return client, db, identity


def png():
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="PNG")
    return output.getvalue()


def test_idempotency_frozen_prices_and_cancel_refunds(accounts, monkeypatch):
    client, db, identity = configure(accounts, monkeypatch)
    workspace = UUID(identity["user"]["id"])
    before = db.get(UserCreditAccount, workspace).balance
    payload = {"prompt": "draw a tree", "idempotency_key": str(uuid4())}
    first = client.post("/image-tasks", json=payload)
    assert first.status_code == 202, first.text
    identifier = first.json()["task_id"]
    assert client.post("/image-tasks", json=payload).json()["task_id"] == identifier
    assert client.get(f"/image-tasks/by-request/{payload['idempotency_key']}").json()["task_id"] == identifier
    assert client.post("/image-tasks", json={**payload, "prompt": "other"}).status_code == 409
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert Decimal(row.snapshot["rate"]) == Decimal("3.25")
    assert db.get(UserCreditAccount, workspace).balance == before - Decimal("3.25")
    client.post(f"/image-tasks/{identifier}/cancel")
    client.post(f"/image-tasks/{identifier}/cancel")
    db.expire_all()
    assert db.get(UserCreditAccount, workspace).balance == before
    assert len(db.scalars(select(ImageTask)).all()) == 1
    assert "encrypted_credentials" not in first.text
    assert "secret" not in first.text


def test_spec_validation_and_read_only(accounts, monkeypatch):
    client, db, identity = configure(accounts, monkeypatch)
    assert client.post("/image-tasks", json={"prompt": "x", "size": "invalid", "idempotency_key": str(uuid4())}).status_code == 422
    assert client.post("/image-tasks", json={"prompt": "x", "count": 2, "idempotency_key": str(uuid4())}).status_code == 422
    assert client.post("/image-tasks", json={"prompt": "x", "operation": "edit", "idempotency_key": str(uuid4())}).status_code == 422
    assert client.post("/image-tasks", json={"prompt": "x", "workspace_id": str(uuid4()), "idempotency_key": str(uuid4())}).status_code == 422


def test_insufficient_balance_and_disabled_default_do_not_submit(accounts, monkeypatch):
    client, db, identity = configure(accounts, monkeypatch)
    account = db.get(UserCreditAccount, UUID(identity["user"]["id"]))
    account.balance = Decimal("2")
    db.commit()
    assert client.post("/image-tasks", json={"prompt": "x", "idempotency_key": str(uuid4())}).status_code == 402
    assert db.scalar(select(ImageTask)) is None
    assert account.balance == Decimal("2")
    settings = client.get("/site/admin/image-models").json()
    assert client.put("/site/admin/image-models", json={**settings, "version": 0}).status_code == 409
    assert client.put("/site/admin/image-models", json={**settings, "generate_model_id": None}).status_code == 200
    assert client.post("/image-tasks", json={"prompt": "x", "idempotency_key": str(uuid4())}).status_code == 503
    assert db.scalar(select(ImageTask)) is None


def test_uncertain_result_keeps_hold_and_never_replays(accounts, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.images import adapters as image_adapters
    client, db, identity = configure(accounts, monkeypatch)
    result = client.post("/image-tasks", json={"prompt": "draw", "idempotency_key": str(uuid4())})
    identifier = result.json()["task_id"]
    attempts = []
    def uncertain(*args):
        attempts.append(1)
        raise ImageUncertain("connection lost")
    monkeypatch.setattr(image_adapters, "generate", uncertain)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    image_tasks.run(identifier, factory)
    image_tasks.run(identifier, factory)
    assert len(attempts) == 1
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert row.status == "uncertain" and not row.settled
    resolved = client.post(f"/site/admin/image-models/tasks/{identifier}/resolve", json={"confirmed_images": 0, "reason": "Confirmed no image delivered"})
    assert resolved.status_code == 200, resolved.text
    assert client.post(f"/site/admin/image-models/tasks/{identifier}/resolve", json={"confirmed_images": 0, "reason": "Confirmed no image delivered"}).status_code == 409


def test_image_format_rejection_and_metadata_strip():
    raw, mime = validate_image(png())
    assert mime == "image/png"
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (16, 16)
    with pytest.raises(Exception):
        validate_image(b"not an image")


def test_successful_generation_can_retry_storage_without_regeneration(accounts, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.files import workspace as workspace_files
    from app.infra import storage
    from app.images import adapters as image_adapters
    from app.models import WorkspaceFile, UsageEvent
    client, db, identity = configure(accounts, monkeypatch)
    attempts = []
    saved = {}
    failing = [True]
    def generate(*args):
        attempts.append(1)
        return [(png(), "image/png")], "upstream-123", {"images": 1}
    def upload(stream, key, mime):
        if failing[0]:
            raise OSError("storage unavailable")
        saved[key] = stream.read()
    monkeypatch.setattr(image_adapters, "generate", generate)
    monkeypatch.setattr(workspace_files, "upload_fileobj", upload)
    monkeypatch.setattr(storage, "delete_object", lambda key: saved.pop(key, None))
    identifier = client.post("/image-tasks", json={"prompt": "draw", "idempotency_key": str(uuid4())}).json()["task_id"]
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    image_tasks.run(identifier, factory)
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert row.status == "save_failed" and row.settled
    assert row.upstream_request_id == "upstream-123"
    assert row.charged_credits == Decimal("3.25")
    assert row.outputs[0]["encrypted_data"]
    assert "encrypted_data" not in client.get(f"/image-tasks/{identifier}").text
    retry = client.post(f"/image-tasks/{identifier}/retry-save")
    assert retry.status_code == 200 and retry.json()["status"] == "saving"
    failing[0] = False
    image_tasks.save_outputs(UUID(identifier), factory)
    image_tasks.run(identifier, factory)
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert row.status == "completed", row.error
    assert len(attempts) == 1
    assert "encrypted_data" not in row.outputs[0]
    assert db.scalar(select(WorkspaceFile)).content_type == "image/png"
    assert len(db.scalars(select(UsageEvent).where(UsageEvent.request_id == row.id)).all()) == 1
    image_tasks.save_outputs(UUID(identifier), factory)
    assert len(saved) == 1


def test_task_is_private_to_its_account(accounts, monkeypatch):
    from test_auth import register
    client, db, _ = configure(accounts, monkeypatch)
    (_, other, _), _ = accounts
    register(other, "image-other@example.com")
    identifier = client.post("/image-tasks", json={"prompt": "draw", "idempotency_key": str(uuid4())}).json()["task_id"]
    assert other.get(f"/image-tasks/{identifier}").status_code == 404
    key = db.get(ImageTask, UUID(identifier)).idempotency_key
    assert other.get(f"/image-tasks/by-request/{key}").status_code == 404
    assert other.post(f"/image-tasks/{identifier}/cancel").status_code == 404
    assert other.get("/site/admin/image-models/tasks").status_code == 403


@pytest.mark.parametrize("revocation", ["session", "suspension"])
def test_queued_task_rechecks_authorization_and_releases_hold(accounts, monkeypatch, revocation):
    from sqlalchemy.orm import sessionmaker
    from app.images import adapters as image_adapters
    from app.models import LoginSession, User
    client, db, identity = configure(accounts, monkeypatch)
    wid, uid = UUID(identity["user"]["id"]), UUID(identity["user"]["id"])
    before = db.get(UserCreditAccount, wid).balance
    identifier = client.post("/image-tasks", json={"prompt": "draw", "idempotency_key": str(uuid4())}).json()["task_id"]
    row = db.get(ImageTask, UUID(identifier))
    if revocation == "session":
        db.delete(db.get(LoginSession, row.session_hash))
    else:
        db.get(User, uid).is_suspended = True
    db.commit()
    calls = []
    monkeypatch.setattr(image_adapters, "generate", lambda *_: calls.append(1))
    image_tasks.run(identifier, sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert row.status == "failed" and row.settled
    assert row.charged_credits == 0
    assert row.error == "登录或账号权限已失效"
    assert db.get(UserCreditAccount, wid).balance == before
    assert calls == []


def test_preparation_credentials_failure_is_actionable_and_redacted(accounts, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.model_catalog import providers as llm_providers
    from app.images import adapters as image_adapters
    client, db, identity = configure(accounts, monkeypatch)
    before = db.get(UserCreditAccount, UUID(identity["user"]["id"])).balance
    identifier = client.post("/image-tasks", json={"prompt": "draw", "idempotency_key": str(uuid4())}).json()["task_id"]
    def decrypt(_):
        raise ValueError("private-key-material")
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", decrypt)
    calls = []
    monkeypatch.setattr(image_adapters, "generate", lambda *_: calls.append(1))
    image_tasks.run(identifier, sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    db.expire_all()
    result = client.get(f"/image-tasks/{identifier}").json()
    assert result["status"] == "failed"
    assert result["summary"] == "图片服务凭据无法读取，请联系管理员检查服务加密配置"
    assert "private-key-material" not in str(result)
    assert db.get(UserCreditAccount, UUID(identity["user"]["id"])).balance == before
    assert calls == []


@pytest.mark.parametrize("cancel_running", [False, True])
def test_partial_results_charge_actual_count_even_after_cancel(accounts, monkeypatch, cancel_running):
    from sqlalchemy.orm import sessionmaker
    from app.files import workspace as workspace_files
    from app.images import adapters as image_adapters
    client, db, identity = configure(accounts, monkeypatch)
    settings = db.scalar(select(SiteModelProvider).where(SiteModelProvider.supports_chat.is_(False)))
    settings.image_config = {**settings.image_config, "max_count": 3}
    db.commit()
    before = db.get(UserCreditAccount, UUID(identity["user"]["id"])).balance
    identifier = client.post("/image-tasks", json={"prompt": "draw", "count": 3, "idempotency_key": str(uuid4())}).json()["task_id"]
    calls = []
    def generate(*_):
        calls.append(1)
        if cancel_running:
            response = client.post(f"/image-tasks/{identifier}/cancel")
            assert response.status_code == 200 and response.json()["cancel_requested"]
        return [(png(), "image/png")], "partial-request", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda *_: None)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    image_tasks.run(identifier, factory)
    image_tasks.run(identifier, factory)
    db.expire_all()
    row = db.get(ImageTask, UUID(identifier))
    assert row.status == ("cancelled" if cancel_running else "partial")
    assert row.charged_credits == Decimal("3.25") and len(row.outputs) == 1
    assert db.get(UserCreditAccount, row.account_id).balance == before - Decimal("3.25")
    assert calls == [1]
