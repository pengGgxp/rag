"""Background environments: activity, read-only usage and configuration snapshots."""
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from app import cloud_computer as cloud, cloud_preview as preview
from app.credits import ensure_account
from app.models import CloudComputerCommand as Command, CloudComputerSession as Computer, CreditLedgerEntry
from test_auth import register
from test_cloud_computer import setup, submit
from test_cloud_preview import prepare


def test_foreground_heartbeat_restarts_exact_idle_window(accounts, monkeypatch):
    client, db, _, _, fake, factory, _, row, computer, *_ = prepare(accounts, monkeypatch)
    start = cloud.now()
    for seconds in (0, 590):
        monkeypatch.setattr(cloud, "now", lambda: start + timedelta(seconds=seconds))
        monkeypatch.setattr(preview, "now", lambda: start + timedelta(seconds=seconds))
        assert client.post(f"/previews/{row.id}/heartbeat", json={}).status_code == 200
    for seconds in (600, 1189, 1190):
        monkeypatch.setattr(cloud, "now", lambda: start + timedelta(seconds=seconds))
        cloud.maintain_one(computer.id, factory)
        db.refresh(computer)
        assert bool(computer.ended_at) == (seconds == 1190)
    assert computer.stop_reason == "idle_timeout" and fake.kills == 1


def test_busy_command_is_not_idle_but_maximum_lifetime_still_applies(accounts, monkeypatch):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="0")
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    db.expire_all()
    computer = db.get(Computer, command.session_id)
    start = cloud.now()
    computer.started_at = computer.last_activity_at = start
    command = db.get(Command, command.id)
    command.status, command.heartbeat_at = "running", start + timedelta(seconds=700)
    fake.result = None
    db.commit()
    monkeypatch.setattr(cloud, "now", lambda: start + timedelta(seconds=700))
    cloud.maintain_one(computer.id, factory)
    db.refresh(computer)
    assert computer.status == "running" and not computer.ended_at
    monkeypatch.setattr(cloud, "now", lambda: start + timedelta(seconds=3600))
    cloud.maintain_one(computer.id, factory)
    db.refresh(computer)
    assert computer.ended_at and computer.stop_reason == "max_lifetime"


def test_usage_pagination_ownership_and_reads_do_not_extend_activity(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    created = [cloud.create_computer(db, access, conversation.id, uuid4(), {"name": f"任务 {i}"})
               for i in range(12)]
    rows = list(db.scalars(select(Computer)))
    before = {row.id: row.last_activity_at for row in rows}
    account = ensure_account(db, access.user_id)
    balance = account.balance
    ledger = list(db.scalars(select(CreditLedgerEntry.id)))
    first = client.get("/cloud-computers?offset=0&limit=10").json()
    second = client.get(f"/cloud-computers?offset={first['next_offset']}&limit=10").json()
    assert len(first["items"]) == 10 and len(second["items"]) == 2 and second["next_offset"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == {item["computer_id"] for item in created}
    for item in first["items"]:
        assert Decimal(item["credits"]) <= Decimal("6") and not item["terminal"]
        client.get(f"/cloud-computers/{item['id']}")
    client.get("/usage")
    db.expire_all()
    assert {row.id: row.last_activity_at for row in db.scalars(select(Computer))} == before
    assert ensure_account(db, access.user_id).balance == balance
    assert list(db.scalars(select(CreditLedgerEntry.id))) == ledger
    other = accounts[0][1]
    register(other, "usage-other@example.com")
    assert other.get("/cloud-computers").json()["items"] == []
    assert other.get(f"/cloud-computers/{rows[0].id}").status_code == 404


def test_versioned_idle_setting_preserves_existing_snapshot(accounts, monkeypatch):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    settings = client.get("/site/admin/cloud-computer").json()
    settings.pop("has_api_key")
    settings["idle_seconds"] = 300
    assert client.put("/site/admin/cloud-computer", json=settings).status_code == 200
    first = cloud.create_computer(db, access, conversation.id, uuid4(), {"name": "旧快照"})
    settings = client.get("/site/admin/cloud-computer").json()
    settings.pop("has_api_key")
    settings["idle_seconds"] = 600
    assert client.put("/site/admin/cloud-computer", json=settings).status_code == 200
    assert client.put("/site/admin/cloud-computer", json=settings).status_code == 409
    second = cloud.create_computer(db, access, conversation.id, uuid4(), {"name": "十分钟"})
    db.expire_all()
    assert db.get(Computer, UUID(first["computer_id"])).snapshot["idle_seconds"] == 300
    assert db.get(Computer, UUID(second["computer_id"])).snapshot["idle_seconds"] == 600
