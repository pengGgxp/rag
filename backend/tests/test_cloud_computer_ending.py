"""Termination causes use real billing and lifecycle code, with a synthetic provider."""
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import cloud_computer as cloud
from app.models import CloudComputerSession as Computer, CreditAccount, CreditLedgerEntry, UsageEvent
from test_cloud_computer import setup, submit


def running(accounts, monkeypatch, seconds=61):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="2")
    command = submit(db, access, conversation)
    cloud.run(command.id, factory)
    db.expire_all()
    row = db.get(Computer, command.session_id)
    clock = cloud.now()
    monkeypatch.setattr(cloud, "now", lambda: clock)
    row.started_at = clock - timedelta(seconds=seconds)
    row.last_activity_at = clock
    db.commit()
    return client, db, conversation, access, fake, factory, row, clock


def test_normal_renewal_keeps_running(accounts, monkeypatch):
    _, db, _, access, fake, factory, row, clock = running(accounts, monkeypatch)
    before = db.get(CreditAccount, access.workspace_id).balance + row.held_credits
    assert cloud.reconcile(db, row, clock) is cloud.ReconcileResult.CONTINUE
    db.commit()
    assert row.stop_reason is None and row.billing_stopped_at is None
    assert row.charged_credits == Decimal("2.0333") and row.held_credits == 20
    assert db.get(CreditAccount, access.workspace_id).balance + row.held_credits == before - row.charged_credits
    cloud.maintenance(factory)
    assert fake.kills == 0


@pytest.mark.parametrize("delay", [601, 1800])
def test_delayed_reconciliation_is_not_insufficient_balance(accounts, monkeypatch, delay):
    client, db, conversation, access, fake, factory, row, _ = running(accounts, monkeypatch, delay)
    account = db.get(CreditAccount, access.workspace_id)
    account.balance = Decimal("95000")
    db.commit()
    fake.kill_uncertain = True
    cloud.maintenance(factory)
    db.expire_all()
    row = db.get(Computer, row.id)
    assert row.status == "stopping" and row.ended_at is None
    assert row.stop_reason == "reservation_expired" and row.charged_credits == 20
    assert row.billed_seconds == 600 and row.held_credits == 0
    payload = client.get(f"/cloud-computers/{row.id}").json()["computer"]
    assert payload["summary"] == "正在结束云电脑"
    assert payload["stop_reason_text"] == "运行状态核对中断"
    assert payload["renewal_required_credits"] is None and not payload["terminal"]
    assert db.get(CreditAccount, access.workspace_id).balance == 95000
    # Repeated end and maintenance cannot replace the cause, charge again or create a VM.
    assert client.post(f"/cloud-computers/{row.id}/end", json={}).status_code == 200
    cloud.maintenance(factory)
    fake.kill_uncertain = False
    cloud.maintenance(factory)
    db.expire_all()
    row = db.get(Computer, row.id)
    assert row.status == "ended" and row.ended_at
    assert row.stop_reason == "reservation_expired"
    payload = client.get(f"/cloud-computers/{row.id}").json()["computer"]
    assert payload["summary"] == "云电脑已结束" and payload["terminal"]
    assert payload["stop_reason_text"] == "运行状态核对中断，云电脑已结束"
    before = (db.get(CreditAccount, access.workspace_id).balance,
        len(db.scalars(select(CreditLedgerEntry)).all()), len(db.scalars(select(UsageEvent)).all()))
    cloud.maintenance(factory)
    db.expire_all()
    assert (db.get(CreditAccount, access.workspace_id).balance,
        len(db.scalars(select(CreditLedgerEntry)).all()), len(db.scalars(select(UsageEvent)).all())) == before
    assert fake.creates == 1 and not fake.instances


def test_positive_balance_can_fail_full_window_reservation(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, row, clock = running(accounts, monkeypatch)
    account = db.get(CreditAccount, access.workspace_id)
    account.balance = 0  # The existing 20-credit hold is separate from available balance.
    db.commit()
    fake.kill_uncertain = True
    assert cloud.reconcile(db, row, clock) is cloud.ReconcileResult.INSUFFICIENT_BALANCE
    row.status = "stopping"
    db.commit()
    assert db.get(CreditAccount, access.workspace_id).balance == Decimal("17.9667")
    cloud.maintenance(factory)
    payload = client.get(f"/cloud-computers/{row.id}").json()["computer"]
    assert payload["summary"] == "正在结束云电脑" and not payload["terminal"]
    assert payload["stop_reason"] == "insufficient_balance"
    assert payload["stop_reason_text"] == "可用积分不足以继续运行"
    assert Decimal(payload["renewal_required_credits"]) == 20
    fake.kill_uncertain = False
    cloud.maintenance(factory)
    db.expire_all()
    payload = client.get(f"/cloud-computers/{row.id}").json()["computer"]
    assert payload["summary"] == "云电脑已结束"
    assert payload["stop_reason"] == "insufficient_balance" and payload["terminal"]


@pytest.mark.parametrize("cause", ["user_requested", "idle_timeout", "max_lifetime", "access_revoked", "provider_ended"])
def test_confirmed_end_preserves_its_cause(accounts, monkeypatch, cause):
    client, db, conversation, access, fake, factory, row, clock = running(accounts, monkeypatch)
    if cause == "user_requested":
        assert client.post(f"/cloud-computers/{row.id}/end", json={}).status_code == 200
    elif cause == "idle_timeout":
        row.started_at = row.last_activity_at = clock - timedelta(seconds=601)
    elif cause == "max_lifetime":
        row.started_at = clock - timedelta(seconds=3601)
    elif cause == "access_revoked":
        from fastapi import HTTPException
        def revoked(*_):
            raise HTTPException(403, "synthetic revoked access")
        monkeypatch.setattr(cloud, "session_access", revoked)
    else:
        fake.instances.clear()
    db.commit()
    cloud.maintenance(factory)
    db.expire_all()
    row = db.get(Computer, row.id)
    assert row.status == "ended" and row.stop_reason == cause
    assert row.held_credits == 0 and row.charged_credits <= 20
    payload = cloud.read(row)
    assert payload["summary"] == "云电脑已结束" and payload["terminal"]
    assert payload["renewal_required_credits"] is None


def test_existing_stop_and_historical_error_are_not_reclassified(accounts, monkeypatch):
    _, db, _, _, _, _, row, clock = running(accounts, monkeypatch)
    row.status, row.billing_stopped_at = "stopping", clock
    row.error = "积分不足，云电脑正在结束"
    db.commit()
    assert cloud.reconcile(db, row, clock, final=True) is cloud.ReconcileResult.STOPPED
    row.status, row.ended_at = "ended", clock
    db.commit()
    assert row.stop_reason is None
    payload = cloud.read(row)
    assert payload["summary"] == "云电脑已结束"
    assert payload["stop_reason_text"] is None and payload["renewal_required_credits"] is None
    assert row.error == "积分不足，云电脑正在结束"  # Preserve historical diagnostics.


def test_first_cause_wins_even_for_a_stale_session(accounts, monkeypatch):
    _, db, _, _, _, factory, row, _ = running(accounts, monkeypatch)
    with factory() as other:
        stale = other.get(Computer, row.id)
        cloud.set_stop_reason(db, row, "user_requested")
        db.commit()
        cloud.set_stop_reason(other, stale, "insufficient_balance")
        other.commit()
        assert stale.stop_reason == "user_requested"


def test_execution_failure_does_not_end_other_commands_or_computer(accounts, monkeypatch):
    _, db, _, _, fake, factory, row, _ = running(accounts, monkeypatch)
    from app.models import Conversation, CloudComputerCommand as Command
    access = cloud.session_access(db, row)
    command = submit(db, access, db.get(Conversation, row.conversation_id))
    def lost(*_):
        raise TimeoutError("synthetic execution interruption")
    original_start = fake.start
    monkeypatch.setattr(fake, "start", lost)
    cloud.run(command.id, factory)
    db.expire_all()
    assert db.get(Command, command.id).status == "uncertain"
    assert db.get(Computer, row.id).status == "ready"
    assert db.get(Computer, row.id).stop_reason is None
    second = submit(db, access, db.get(Conversation, row.conversation_id))
    monkeypatch.setattr(fake, "start", original_start)
    cloud.run(second.id, factory)
    db.expire_all()
    assert db.get(Command, second.id).status == "completed"
    assert db.get(Computer, row.id).status == "ready"
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(Computer, row.id).stop_reason is None
    assert cloud.read(db.get(Computer, row.id))["summary"] == "云电脑运行中"


def test_startup_failure_keeps_internal_errors_out_of_summary(accounts, monkeypatch):
    _, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="2")
    def unavailable(*_):
        raise RuntimeError("synthetic private diagnostic")
    monkeypatch.setattr(fake, "prepare", unavailable)
    created = cloud.create_computer(db, access, conversation.id, uuid4(), {"name": "准备失败测试"})
    assert created["uncertain"]
    db.expire_all()
    row = db.get(Computer, UUID(created["computer_id"]))
    assert row.status == "uncertain"
    row.error = "synthetic private diagnostic"
    assert cloud.read(row)["summary"] == "正在核对云电脑状态"
    assert "synthetic private diagnostic" not in str(cloud.read(row))
