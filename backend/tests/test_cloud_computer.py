import hashlib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import auth, cloud_computer as cloud
from app.config import get_settings
from app.models import CloudComputerCommand as Command, CloudComputerSession as Computer, CreditAccount, CreditLedgerEntry, Conversation, User, UsageEvent
from test_auth import register, share, switch
from test_resources import resource_store, upload, access_for


class FakeCloud:
    def __init__(self):
        self.instances = {}
        self.creates = self.starts = self.kills = self.writes = 0
        self.skill_clears = 0
        self.create_uncertain = self.kill_uncertain = False
        self.outputs = {"result.txt": b"synthetic output"}
        self.result = {"exit_code": 0, "stdout": "done", "stderr": ""}
        self.cancelled = []

    def create(self, identifier):
        self.creates += 1
        key = str(identifier)
        self.instances[key] = {"files": {}, "started_at": cloud.now()}
        if self.create_uncertain:
            raise TimeoutError()
        return key, self.instances[key]["started_at"]

    def find(self, identifier):
        return [(key, value["started_at"]) for key, value in self.instances.items() if key == str(identifier)]

    def connect(self, identifier): return self.instances[identifier]
    def prepare(self, sandbox): pass
    def exists(self, identifier): return identifier in self.instances

    def kill(self, identifier):
        self.kills += 1
        if self.kill_uncertain:
            return False
        self.instances.pop(identifier, None)
        return True

    def write(self, sandbox, path, data):
        self.writes += 1
        sandbox["files"][path] = data

    def clear_skills(self, sandbox):
        self.skill_clears += 1
        sandbox.setdefault("skills", {}).clear()

    def write_skill(self, sandbox, path, data):
        sandbox.setdefault("skills", {})[path] = data

    def remove(self, sandbox, path): sandbox["files"].pop(path, None)

    def start(self, sandbox, identifier, command):
        self.starts += 1
        sandbox["files"].update(self.outputs)
        return 42

    def poll(self, sandbox, identifier): return self.result
    def cancel(self, sandbox, identifier, pid): self.cancelled.append(identifier)

    def manifest(self, sandbox):
        return {path: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for path, data in sandbox["files"].items()}

    def read(self, sandbox, path, expected): return sandbox["files"][path]


def setup(accounts, monkeypatch, rate="0.6"):
    (client, member, other), db = accounts
    identity = register(client)
    user = db.get(User, UUID(identity["user"]["id"]))
    user.is_site_admin = True
    db.commit()
    monkeypatch.setattr(get_settings(), "site_config_encryption_key", SimpleNamespace(get_secret_value=lambda: Fernet.generate_key().decode()))
    # Use a fixed generated test-only key for repeated encrypt/decrypt calls.
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(get_settings(), "site_config_encryption_key", SimpleNamespace(get_secret_value=lambda: key))
    from app import storage_backend
    from app.models import SiteStorageSettings
    storage_row = db.get(SiteStorageSettings, 1)
    if storage_row is None:
        storage_row = SiteStorageSettings(id=2, version=1, provider="oss", bucket="test-bucket", region="cn-hangzhou",
            encrypted_secret_id="test-id", encrypted_secret_key="test-secret", active=True)
        db.add(storage_row); db.commit()
    monkeypatch.setattr(storage_backend, "active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: SimpleNamespace(
        bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: None)
    body = {"version": 0, "enabled": True, "region": "cn-hangzhou", "template": "test-template", "specification": "test-only",
            "api_key": "synthetic-api-key", "credits_per_minute": rate, "oss_role_arn": "acs:ram::123:role/test"}
    response = client.put("/site/admin/cloud-computer", json=body)
    assert response.status_code == 200, response.text
    conversation = Conversation(workspace_id=UUID(identity["workspace_id"]), owner_id=user.id)
    db.add(conversation)
    db.commit()
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    return client, db, conversation, access_for(client, db), fake, sessionmaker(bind=db.get_bind())


def submit(db, access, conversation, command="echo test", request=None, inputs=None):
    return cloud.submit(db, access, conversation.id, request or uuid4(), {"command": command})


def enable_execution(db, access, monkeypatch):
    from app.models import CloudComputerSettings
    settings = db.get(CloudComputerSettings, 1)
    if settings is None:
        db.add(CloudComputerSettings(id=1, version=1, enabled=True,
            config=cloud.Configuration(region="cn-hangzhou", template="test", specification="test", credits_per_minute=0,
                oss_role_arn="acs:ram::123:role/test").model_dump(mode="json"),
            encrypted_api_key="test-only"))
    conversation = Conversation(workspace_id=access.workspace_id, owner_id=access.user_id)
    db.add(conversation); db.commit()
    fake = FakeCloud()
    fake.outputs = {}
    from test_cloud_computer_mount import MemoryMount, configure_mount
    from app.shared_resources import prefix
    fake.storage = MemoryMount()
    configure_mount(db, monkeypatch, fake.storage)
    original_start = fake.start
    def mounted_start(sandbox, identifier, command):
        for path, data in fake.outputs.items():
            fake.storage.objects[prefix(access.workspace_id) + path] = data
        return original_start(sandbox, identifier, command)
    monkeypatch.setattr(fake, "start", mounted_start)
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    monkeypatch.setattr(cloud, "dispatch", lambda identifier: cloud.run(identifier, factory))
    def execute(arguments):
        return cloud.execute(db, access, conversation.id, uuid4(), arguments, None)
    return fake, execute


def test_settings_admin_secret_version_validation(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    value = client.get("/site/admin/cloud-computer")
    assert "synthetic-api-key" not in value.text
    assert "encrypted_api_key" not in value.text
    data = value.json()
    assert data["has_api_key"] and data["idle_seconds"] == 600
    assert client.put("/site/admin/cloud-computer", json={**{k: v for k, v in data.items() if k != "has_api_key"}, "version": 0}).status_code == 409
    member = accounts[0][1]
    register(member, "member@example.com")
    assert member.get("/site/admin/cloud-computer").status_code == 403
    data.pop("has_api_key")
    assert client.put("/site/admin/cloud-computer", json=data).json()["has_api_key"]
    data["version"] += 1
    assert client.put("/site/admin/cloud-computer", json={**data, "max_seconds": 300, "idle_seconds": 600}).status_code == 422
    assert client.put("/site/admin/cloud-computer", json={**data, "credits_per_minute": -1}).status_code == 422
    assert client.put("/site/admin/cloud-computer", json={**data, "clear_api_key": True}).status_code == 422
    assert client.put("/site/admin/cloud-computer", json={**data, "clear_api_key": True, "enabled": False}).json()["has_api_key"] is False


def test_connection_check_never_creates_instance(accounts, monkeypatch):
    client, _, _, _, fake, _ = setup(accounts, monkeypatch)
    assert client.post("/site/admin/cloud-computer/check", json={}).json() == {"connected": True, "template_verified": False}
    assert fake.creates == 0


def test_reuse_idempotency_and_outputs(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    request = uuid4()
    command = submit(db, access, conversation, request=request)
    original = command.id
    assert submit(db, access, conversation, request=request).id == original
    cloud.run(command.id, factory)
    db.expire_all()
    command = db.get(Command, original)
    assert command.status == "completed", command.result
    assert "generated_files" not in command.result
    second = submit(db, access, conversation, command="echo again")
    cloud.run(second.id, factory)
    assert fake.creates == 1 and fake.starts == 2 and fake.writes == 0
    assert len(db.scalars(select(Computer)).all()) == 1
    listing = client.get(f"/conversations/{conversation.id}/cloud-computer/commands")
    assert "synthetic-api-key" not in listing.text and "provider_id" not in listing.text


def test_replay_returns_original_without_preparing_inputs(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, _ = setup(accounts, monkeypatch)
    request = uuid4()
    original = submit(db, access, conversation, request=request)

    def unavailable(*args):
        raise HTTPException(404, "resource no longer available")

    monkeypatch.setattr("app.workspace_files.resolve_input", unavailable)
    assert submit(db, access, conversation, request=request).id == original.id
    assert fake.creates == 0


def test_initial_price_is_one_credit_and_remains_disabled():
    draft = cloud.settings_read(None)
    assert Decimal(str(draft["credits_per_minute"])) == Decimal("1")
    assert draft["enabled"] is False
    assert draft["blocked_hosts"] == []


def test_balance_reservation_and_cumulative_rounding(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="0.0001")
    before = db.get(CreditAccount, access.workspace_id).balance
    command = submit(db, access, conversation)
    row = db.get(Computer, command.session_id)
    start = cloud.now()
    row.started_at = start
    db.commit()
    assert cloud.charge_at(row, start+timedelta(seconds=29))[1] == Decimal("0")
    assert cloud.charge_at(row, start+timedelta(seconds=30))[1] == Decimal("0.0001")
    cloud.reconcile(db, row, start+timedelta(seconds=60))
    cloud.reconcile(db, row, start+timedelta(seconds=90))
    cloud.reconcile(db, row, start+timedelta(seconds=120), final=True)
    db.commit()
    assert row.charged_credits == Decimal("0.0002")
    assert db.get(CreditAccount, access.workspace_id).balance == before - Decimal("0.0002")
    assert db.get(CreditAccount, access.workspace_id).lifetime_spent == Decimal("0.0002")
    assert sum(event.credits for event in db.scalars(select(UsageEvent))) == Decimal("0.0002")


def test_insufficient_balance_does_not_create_or_dispatch(accounts, monkeypatch):
    _, db, conversation, access, fake, _ = setup(accounts, monkeypatch)
    account = db.get(CreditAccount, access.workspace_id)
    account.balance = 1
    db.commit()
    with pytest.raises(HTTPException) as error:
        submit(db, access, conversation)
    assert error.value.status_code == 402
    db.rollback()
    assert db.scalar(select(Computer)) is None and fake.creates == 0


def test_owner_workspace_role_and_concurrency(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    member = accounts[0][1]
    identity = register(member, "member@example.com")
    assert member.get(f"/conversations/{conversation.id}/cloud-computer").status_code == 404
    assert member.post(f"/conversations/{conversation.id}/cloud-computer/end", json={}).status_code == 404
    submit(db, access, conversation)
    other = Conversation(workspace_id=access.workspace_id, owner_id=access.user_id)
    db.add(other); db.commit()
    with pytest.raises(HTTPException) as error:
        submit(db, access, other)
    assert error.value.status_code == 409
    db.rollback()
    share(client, "member@example.com", "viewer"); switch(member, str(access.workspace_id))
    own = Conversation(workspace_id=access.workspace_id, owner_id=UUID(identity["user"]["id"]))
    db.add(own); db.commit()
    with pytest.raises(HTTPException) as error:
        submit(db, access_for(member, db), own)
    assert error.value.status_code == 403


def test_idle_destroy_and_failed_destroy_not_billed(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    before = db.get(CreditAccount, access.workspace_id).balance
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    db.expire_all()
    row = db.get(Computer, command.session_id)
    start = cloud.now()-timedelta(seconds=1000)
    row.started_at = row.last_activity_at = start
    db.commit()
    fake.kill_uncertain = True
    cloud.maintenance(factory)
    db.expire_all()
    row = db.get(Computer, row.id)
    assert row.status == "stopping" and row.ended_at is None
    assert cloud.charge_at(row, cloud.now())[1] == Decimal("6")
    fake.kill_uncertain = False
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(Computer, row.id).status == "ended"
    assert db.get(CreditAccount, access.workspace_id).balance == before-Decimal("6")
    cloud.maintenance(factory)
    assert db.get(CreditAccount, access.workspace_id).balance == before-Decimal("6")


def test_unknown_create_reconciles_without_second_create(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    fake.create_uncertain = True
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    db.expire_all()
    row = db.get(Computer, command.session_id)
    assert row.status == "uncertain" and fake.creates == 1
    row.created_at = cloud.now()-timedelta(seconds=120)
    db.commit()
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(Computer, row.id).ended_at and fake.creates == 1 and fake.starts == 0


def test_price_changes_frozen_and_ended_request_not_replayed(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    request = uuid4()
    command = submit(db, access, conversation, request=request)
    cloud.run(command.id, factory)
    body = client.get("/site/admin/cloud-computer").json(); body.pop("has_api_key")
    assert client.put("/site/admin/cloud-computer", json={**body, "credits_per_minute": 99}).status_code == 200
    db.expire_all()
    row = db.get(Computer, command.session_id)
    assert Decimal(str(row.snapshot["credits_per_minute"])) == Decimal("0.6")
    assert client.post(f"/conversations/{conversation.id}/cloud-computer/end", json={}).status_code == 200
    cloud.maintenance(factory)
    assert submit(db, access, conversation, request=request).id == command.id
    assert fake.creates == 1 and fake.starts == 1


def test_unknown_command_never_replayed(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    def lost(*args):
        fake.starts += 1
        raise TimeoutError()
    monkeypatch.setattr(fake, "start", lost)
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    cloud.run(command.id, factory)
    db.expire_all()
    assert db.get(Command, command.id).status == "uncertain"
    assert fake.starts == 1


def test_backend_rejects_csrf_and_arbitrary_config_fields(accounts, monkeypatch):
    client, _, _, _, _, _ = setup(accounts, monkeypatch)
    body = client.get("/site/admin/cloud-computer").json(); body.pop("has_api_key")
    assert client.put("/site/admin/cloud-computer", json={**body, "api_url": "http://127.0.0.1"}).status_code == 422
    assert client.put("/site/admin/cloud-computer", json={**body, "region": "example.com/x"}).status_code == 422
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put("/site/admin/cloud-computer", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf


def test_end_destroys_instance_even_when_process_cancellation_fails(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    db.expire_all()
    command = db.get(Command, command.id)
    command.status = "running"
    command.heartbeat_at = cloud.now()
    db.commit()
    def unavailable(*args):
        raise TimeoutError()
    monkeypatch.setattr(fake, "cancel", unavailable)
    assert client.post(f"/conversations/{conversation.id}/cloud-computer/end", json={}).status_code == 200
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(Computer, command.session_id).ended_at is not None
    assert fake.kills == 1 and not fake.instances
    assert db.get(Command, command.id).status == "cancelled"


def test_lost_worker_recovers_result_without_reexecuting(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    command = submit(db, access, conversation)
    row = db.get(Computer, command.session_id)
    row.provider_id, row.started_at = fake.create(row.id)
    row.status = "running"
    command.status = "running"
    command.heartbeat_at = cloud.now()-timedelta(seconds=90)
    fake.start(fake.connect(row.provider_id), command.id, "synthetic")
    db.commit()
    cloud.maintenance(factory)
    db.expire_all()
    command = db.get(Command, command.id)
    assert command.status == "completed" and "generated_files" not in command.result
    assert fake.starts == 1
    cloud.maintenance(factory)
    assert fake.starts == 1
