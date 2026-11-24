"""Opt-in PostgreSQL migration, concurrent reason and settlement acceptance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4, uuid5

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app import cloud_computer as cloud

from app.identity import auth
from app.credits import authorization_hold, ensure_account
from app.models import (
    CloudComputerSession as Computer, Conversation, UserCreditAccount, CreditLedgerEntry,
    LoginSession, UsageEvent,
)
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud
from test_personal_accounts_postgres import account, insert, legacy_database, workspace
from test_tool_runtime_migrations import migration


def seed(engine):
    with Session(engine) as db:
        user = auth.create_user(db, f"cloud-ending-{uuid4().hex}@example.com", "Cloud ending")
        account_id = user.id
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id,
            csrf_token=uuid4().hex, expires_at=auth.now() + timedelta(hours=1)))
        conversation = Conversation(account_id=account_id, owner_id=user.id)
        db.add(conversation)
        db.flush()
        row = Computer(account_id=account_id, user_id=user.id, conversation_id=conversation.id,
            session_hash=session_hash, status="ready", provider_id=uuid4().hex, encrypted_api_key="synthetic",
            snapshot={"credits_per_minute": "2", "idle_seconds": 600, "max_seconds": 3600, "version": 1},
            held_credits=Decimal("20"), started_at=auth.now() - timedelta(seconds=601),
            last_activity_at=auth.now())
        db.add(row)
        db.flush()
        ensure_account(db, account_id).balance = Decimal("100000")
        authorization_hold(db, account_id, user.id, uuid5(row.id, "hold:0"), Decimal("20"))
        db.commit()
        return row.id, account_id, auth.Access(user.id, session_hash)


def test_concurrent_end_and_maintenance_charge_once(auth_postgres, monkeypatch):
    engine = auth_postgres
    identifier, account_id, access = seed(engine)
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
        assert db.get(UserCreditAccount, account_id).balance == 99980
        assert db.get(UserCreditAccount, account_id).lifetime_spent == 20
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.account_id == account_id)))
        charges = list(db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account_id, CreditLedgerEntry.kind == "usage_settlement")))
        assert len(events) == len(charges) == 1 and events[0].credits == 20
        reason, sequence = row.stop_reason, row.billing_sequence
    cloud.maintain_one(identifier, factory)
    with factory() as db:
        row = db.get(Computer, identifier)
        assert (row.stop_reason, row.billing_sequence) == (reason, sequence)
        assert db.get(UserCreditAccount, account_id).balance == 99980
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


def legacy_cloud(db, account_id, user_id, conversation_id, **values):
    return insert(db, "cloud_computer_sessions", workspace_id=account_id, user_id=user_id,
        conversation_id=conversation_id, session_hash="legacy", snapshot={}, encrypted_api_key="",
        manifest={}, held_credits=20, charged_credits=0, billed_seconds=0, billing_sequence=0, **values)


def test_additive_migration_keeps_legacy_rows_and_billing(legacy_database):
    db, _ = legacy_database
    user_id = account(db, f"legacy-cloud-{uuid4().hex}")
    account_id = workspace(db, "Legacy cloud billing", user_id)
    conversation_id = insert(db, "conversations", workspace_id=account_id, owner_id=user_id,
        title="Legacy", next_sequence=1)
    identifier = legacy_cloud(db, account_id, user_id, conversation_id, status="ended",
        ended_at=auth.now(), error="积分不足，云电脑正在结束")
    insert(db, "credit_accounts", workspace_id=account_id, balance=99980, lifetime_granted=100000,
        lifetime_spent=20)
    insert(db, "credit_ledger", workspace_id=account_id, amount=-20, balance_after=99980,
        kind="usage_settlement", idempotency_key=f"legacy-cloud:{identifier}", metadata={})

    def snapshot():
        return {
            table: db.execute(text(f'SELECT {columns} FROM {table} WHERE workspace_id=:id '
                + ("ORDER BY id" if table != "credit_accounts" else "")),
                {"id": account_id}).mappings().all()
            for table, columns in (
                ("credit_accounts", "*"), ("credit_ledger", "*"), ("usage_events", "*"),
                ("cloud_computer_sessions", "id, workspace_id, status, charged_credits, held_credits, error, ended_at"),
            )
        }
    before = snapshot()
    revision = migration("0073_cloud_stop_reason")
    with Operations.context(MigrationContext.configure(db.connection())):
        revision.downgrade()
        revision.upgrade()
    assert snapshot() == before
    assert db.scalar(text("SELECT stop_reason FROM cloud_computer_sessions WHERE id=:id"),
        {"id": identifier}) is None
