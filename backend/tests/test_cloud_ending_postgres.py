"""Opt-in PostgreSQL migration, concurrent reason and settlement acceptance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4, uuid5

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app import auth, cloud_computer as cloud
from app.credits import authorization_hold, ensure_account
from app.models import (
    CloudComputerSession as Computer, Conversation, CreditAccount, CreditLedgerEntry,
    LoginSession, UsageEvent,
)
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud


def seed(engine):
    with Session(engine) as db:
        user, workspace_id = auth.create_user(db, f"cloud-ending-{uuid4().hex}@example.com", "Cloud ending")
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id, workspace_id=workspace_id,
            csrf_token=uuid4().hex, expires_at=auth.now() + timedelta(hours=1)))
        conversation = Conversation(workspace_id=workspace_id, owner_id=user.id)
        db.add(conversation)
        db.flush()
        row = Computer(workspace_id=workspace_id, user_id=user.id, conversation_id=conversation.id,
            session_hash=session_hash, status="ready", provider_id=uuid4().hex, encrypted_api_key="synthetic",
            snapshot={"credits_per_minute": "2", "idle_seconds": 600, "max_seconds": 3600, "version": 1},
            held_credits=Decimal("20"), started_at=auth.now() - timedelta(seconds=601),
            last_activity_at=auth.now())
        db.add(row)
        db.flush()
        ensure_account(db, workspace_id).balance = Decimal("100000")
        authorization_hold(db, workspace_id, user.id, uuid5(row.id, "hold:0"), Decimal("20"))
        db.commit()
        return row.id, workspace_id, auth.Access(user.id, workspace_id, "admin", session_hash)


def test_concurrent_end_and_maintenance_charge_once(auth_postgres, monkeypatch):
    engine = auth_postgres
    identifier, workspace_id, access = seed(engine)
    factory = sessionmaker(bind=engine)
    fake = FakeCloud()
    with factory() as db:
        row = db.get(Computer, identifier)
        conversation_id = row.conversation_id
        fake.instances[row.provider_id] = {"files": {}, "started_at": row.started_at}
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    barrier = Barrier(4)
    def finish(index):
        barrier.wait(timeout=10)
        if index == 0:
            with factory() as db:
                cloud.end(identifier, db, access)
        else:
            cloud.maintain_one(identifier, factory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(finish, range(4)))
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        row = db.get(Computer, identifier)
        assert row.status == "ended" and row.ended_at and row.held_credits == 0
        assert row.stop_reason in ("user_requested", "reservation_expired")
        assert row.charged_credits == 20 and row.billed_seconds == 600
        assert db.get(CreditAccount, workspace_id).balance == 99980
        assert db.get(CreditAccount, workspace_id).lifetime_spent == 20
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace_id)))
        charges = list(db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == workspace_id, CreditLedgerEntry.kind == "usage_settlement")))
        assert len(events) == len(charges) == 1 and events[0].credits == 20
        reason, sequence = row.stop_reason, row.billing_sequence
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        row = db.get(Computer, identifier)
        assert (row.stop_reason, row.billing_sequence) == (reason, sequence)
        assert db.get(CreditAccount, workspace_id).balance == 99980
    assert fake.kills == 1 and fake.creates == 0


def test_concurrent_first_reason_is_not_overwritten(auth_postgres):
    identifier, _, _ = seed(auth_postgres)
    barrier = Barrier(4)
    def record(reason):
        with Session(auth_postgres) as db:
            row = db.get(Computer, identifier)
            barrier.wait(timeout=10)
            cloud.set_stop_reason(db, row, reason)
            db.commit()
            return row.stop_reason
    with ThreadPoolExecutor(max_workers=4) as pool:
        reasons = list(pool.map(record, ["user_requested", "idle_timeout", "reservation_expired", "insufficient_balance"]))
    assert len(set(reasons)) == 1
    with Session(auth_postgres) as db:
        row = db.get(Computer, identifier)
        row.status, row.ended_at = "ended", auth.now()
        db.commit()


def test_additive_migration_keeps_legacy_rows_and_billing(auth_postgres, monkeypatch):
    engine = auth_postgres
    identifier, workspace_id, _ = seed(engine)
    def snapshot(connection):
        return {
            table: connection.execute(text(f'SELECT {columns} FROM {table} ORDER BY '
                + ("workspace_id" if table == "credit_accounts" else "id"))).mappings().all()
            for table, columns in (
                ("credit_accounts", "*"), ("credit_ledger", "*"), ("usage_events", "*"),
                ("cloud_computer_sessions", "id, workspace_id, status, charged_credits, held_credits, error, ended_at"),
            )
        }
    # The fixture only admits rag_auth_test_* databases; no developer or production DB.
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        connection.execute(text("UPDATE cloud_computer_sessions SET error=:error, status='ended', ended_at=now() WHERE id=:id"),
            {"error": "积分不足，云电脑正在结束", "id": identifier})
        before = snapshot(connection)
    config = Config("alembic.ini")
    command.downgrade(config, "0072_agent_tool_jobs")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert snapshot(connection) == before
        assert connection.scalar(text("SELECT stop_reason FROM cloud_computer_sessions WHERE id=:id"), {"id": identifier}) is None
    with Session(engine) as db:
        row = db.get(Computer, identifier)
        assert cloud.read(row)["summary"] == "云电脑已结束"
        assert row.stop_reason is None and db.get(CreditAccount, workspace_id).balance == 99980
