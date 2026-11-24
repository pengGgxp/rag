"""Explicit lifecycle, routing and recovery; all provider operations are synthetic."""
import json
import threading
import time
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cloud_computer import runtime as cloud
from app.models import ChatMessage, Conversation, CreditLedgerEntry, UserCreditAccount
from app.integrations.runtime import ExtensionRuntime
from test_cloud_computer import setup
from test_resources import resource_store


def create(db, access, conversation, *, request=None, name="测试任务"):
    return cloud.create_computer(db, access, conversation.id, request or uuid4(), {"name": name})


def test_explicit_creation_replay_and_different_intents(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    request = uuid4()
    one = create(db, access, conversation, request=request)
    ledger = list(db.scalars(select(CreditLedgerEntry.id)))
    same = create(db, access, conversation, request=request)
    assert same["computer_id"] == one["computer_id"] and fake.creates == 1
    assert list(db.scalars(select(CreditLedgerEntry.id))) == ledger
    with pytest.raises(HTTPException) as conflict:
        create(db, access, conversation, request=request, name="另一个任务")
    assert conflict.value.status_code == 409
    db.rollback()
    two = create(db, access, conversation)
    assert two["computer_id"] != one["computer_id"] and fake.creates == 2
    page = client.get("/cloud-computers?limit=1").json()
    assert len(page["items"]) == 1 and page["next_offset"] == 1
    assert "concurrency" not in client.get("/site/admin/cloud-computer").json()
    assert client.get(f"/conversations/{conversation.id}/cloud-computer").status_code == 404


def test_missing_id_never_creates_and_cross_conversation_reuses(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    with pytest.raises(HTTPException) as missing:
        cloud.submit(db, access, conversation.id, uuid4(), {"command": "echo no"})
    assert missing.value.status_code == 422 and fake.creates == 0
    db.rollback()
    one = create(db, access, conversation)
    other = Conversation(account_id=access.user_id, owner_id=access.user_id)
    db.add(other); db.commit()
    commands = [cloud.submit(db, access, other.id, uuid4(),
        {"computer_id": one["computer_id"], "command": f"echo {index}"}) for index in range(2)]
    assert len({command.id for command in commands}) == 2
    for command in commands:
        cloud.run(command.id, factory)
    assert fake.creates == 1 and fake.starts == 2
    assert all(item["computer_id"] == one["computer_id"]
        for item in client.get(f"/cloud-computers/{one['computer_id']}/commands").json()["items"])


def test_unknown_create_then_end_without_provider_id_recovers(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    fake.create_uncertain = True
    one = create(db, access, conversation)
    identifier = UUID(one["computer_id"])
    assert one["uncertain"] and not db.get(cloud.Computer, identifier).provider_id
    assert client.post(f"/cloud-computers/{identifier}/end", json={}).status_code == 200
    row = db.get(cloud.Computer, identifier, populate_existing=True)
    row.created_at = cloud.now() - timedelta(seconds=120)
    db.commit()
    fake.create_uncertain = False
    two = create(db, access, conversation)
    cloud.maintenance(factory)
    db.expire_all()
    row = db.get(cloud.Computer, identifier)
    assert row.ended_at and row.stop_reason == "user_requested"
    assert not db.get(cloud.Computer, UUID(two["computer_id"])).ended_at
    assert fake.creates == 2 and fake.kills == 1
    ledger = list(db.scalars(select(CreditLedgerEntry.id)))
    cloud.maintenance(factory)
    assert list(db.scalars(select(CreditLedgerEntry.id))) == ledger


def test_unknown_creation_can_recover_ready_without_replay(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    fake.create_uncertain = True
    one = create(db, access, conversation)
    row = db.get(cloud.Computer, UUID(one["computer_id"]))
    row.created_at = cloud.now() - timedelta(seconds=120)
    db.commit()
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(cloud.Computer, row.id).status == "ready"
    assert fake.creates == 1 and fake.starts == 0


def test_provider_disappearance_and_delayed_stop_are_independent(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    one, two = [create(db, access, conversation) for _ in range(2)]
    client.post(f"/cloud-computers/{one['computer_id']}/end", json={})
    fake.kill_uncertain = True
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(cloud.Computer, UUID(one["computer_id"])).status == "stopping"
    assert db.get(cloud.Computer, UUID(two["computer_id"])).status == "ready"
    fake.kill_uncertain = False
    cloud.maintenance(factory)
    fake.instances.pop(two["computer_id"])
    cloud.maintenance(factory)
    db.expire_all()
    assert all(db.get(cloud.Computer, UUID(item["computer_id"])).ended_at for item in (one, two))


def test_creation_more_than_old_maximum(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="0")
    identifiers = {create(db, access, conversation)["computer_id"] for _ in range(103)}
    assert len(identifiers) == fake.creates == 103
    page = client.get("/cloud-computers?active_only=true&limit=100").json()
    assert len(page["items"]) == 100 and page["next_offset"] == 100


def test_workspace_and_user_access_stay_enforced(accounts, monkeypatch, resource_store):
    from test_auth import register
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    one = create(db, access, conversation)
    member = accounts[0][1]
    register(member, "other-cloud@example.com")
    assert member.get(f"/cloud-computers/{one['computer_id']}").status_code == 404
    assert member.post(f"/cloud-computers/{one['computer_id']}/end", json={}).status_code == 404
    assert member.get("/cloud-computers").json()["items"] == []


def test_model_tools_create_command_query_and_stop(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    message = ChatMessage(conversation_id=conversation.id, sequence=1, role="assistant", content="", request_id=uuid4())
    db.add(message); db.commit()
    def runtime():
        return ExtensionRuntime(factory, access, message_id=message.id, request_id=message.request_id)
    created = runtime().execute("create_cloud_computer", {"name": "生成报告"}, call_id="create-1")
    identifier = created["computer_id"]
    assert runtime().execute("run_command", {"command": "echo ok"}, call_id="missing")["error"]
    result = runtime().execute("run_command", {"command": "echo ok", "computer_id": identifier}, call_id="run-1")
    assert result["computer_id"] == identifier and result["status"] == "completed"
    assert runtime().execute("get_cloud_computer", {"computer_id": identifier})["cloud_computer"]["status"] == "ready"
    assert runtime().execute("list_cloud_computers", {})["items"][0]["computer_id"] == identifier
    stopped = runtime().execute("end_cloud_computer", {"computer_id": identifier}, call_id="end-1")
    assert stopped["cloud_computer"]["status"] == "stopping"
    cloud.maintenance(factory)
    assert fake.creates == fake.starts == fake.kills == 1


def test_background_slow_instance_does_not_block_other_instances(accounts, monkeypatch, resource_store):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="0")
    first, second = [UUID(create(db, access, conversation)["computer_id"]) for _ in range(2)]
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    original = cloud._maintain
    def slow(db, identifier):
        if identifier == first:
            entered.set()
            release.wait(5)
        else:
            observed.set()
        original(db, identifier)
    monkeypatch.setattr(cloud, "_maintain", slow)
    try:
        cloud.maintenance(factory, wait=False)
        assert entered.wait(2) and observed.wait(2)
        assert not release.is_set()
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while cloud._scheduled and time.monotonic() < deadline:
            time.sleep(.02)
    assert not cloud._scheduled
