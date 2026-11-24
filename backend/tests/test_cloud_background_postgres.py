"""Exact idle cutoff and idempotent billing against a disposable PostgreSQL DB."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.cloud_computer import runtime as cloud, preview

from app.identity import auth
from app.models import CloudComputerCommand, CloudPreview, UserCreditAccount, CreditLedgerEntry, UsageEvent
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud
from test_cloud_ending_postgres import seed


def test_heartbeat_idle_cutoff_and_usage_are_read_only(auth_postgres, monkeypatch):
    factory = sessionmaker(bind=auth_postgres)
    identifier, workspace, access = seed(auth_postgres)
    clock = auth.now()
    monkeypatch.setattr(cloud, "now", lambda: clock)
    monkeypatch.setattr(preview, "now", lambda: clock)
    settings = preview.get_settings().model_copy(update={"preview_enabled": True})
    monkeypatch.setattr(preview, "get_settings", lambda: settings)
    fake = FakeCloud()
    monkeypatch.setattr(fake, "preview_alive", lambda *_: True, raising=False)
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    with factory() as db:
        row = db.get(cloud.Computer, identifier)
        row.started_at = row.last_activity_at = clock
        fake.instances[row.provider_id] = {"files": {}, "started_at": clock}
        command = CloudComputerCommand(session_id=row.id, request_id=uuid4(), digest=uuid4().hex,
            payload={}, status="completed")
        db.add(command); db.flush()
        shown = CloudPreview(session_id=row.id, command_id=command.id, name="临时验收",
            port=8080, status="ready")
        db.add(shown); db.commit()
        preview.heartbeat(shown.id, db, access)
        assert auth.aware(shown.lease_until) == clock + timedelta(seconds=60)
        before = (row.last_activity_at, db.get(UserCreditAccount, workspace).balance)
        for _ in range(3):
            cloud.computer_list(offset=0, limit=10, active_only=False, db=db, access=access)
            preview.detail(shown.id, db, access)
        assert (row.last_activity_at, db.get(UserCreditAccount, workspace).balance) == before
    monkeypatch.setattr(cloud, "now", lambda: clock + timedelta(seconds=599))
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        assert not db.get(cloud.Computer, identifier).ended_at
    monkeypatch.setattr(cloud, "now", lambda: clock + timedelta(seconds=600))
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        row = db.get(cloud.Computer, identifier)
        assert row.ended_at and row.stop_reason == "idle_timeout"
        assert row.billed_seconds == 600 and row.charged_credits == Decimal("20")
        assert row.held_credits == 0
        settled = (db.get(UserCreditAccount, workspace).balance,
            list(db.scalars(select(CreditLedgerEntry.id).where(CreditLedgerEntry.account_id == workspace))))
        assert sum(db.scalars(select(UsageEvent.credits).where(UsageEvent.account_id == workspace))) == 20
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        assert (db.get(UserCreditAccount, workspace).balance,
            list(db.scalars(select(CreditLedgerEntry.id).where(CreditLedgerEntry.account_id == workspace)))) == settled
    assert fake.kills == 1
