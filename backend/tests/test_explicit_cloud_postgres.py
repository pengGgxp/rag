from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session, sessionmaker

from app import cloud_computer as cloud

from app.identity import auth
from app.models import CloudComputerSettings, Conversation, LoginSession, UserCreditAccount
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud
from test_cloud_ending_postgres import legacy_cloud
from test_personal_accounts_postgres import account, insert, legacy_database, workspace
from test_tool_runtime_migrations import migration


def seed(engine, monkeypatch):
    with Session(engine) as db:
        user = auth.create_user(db, f"explicit-{uuid4().hex}@example.com", "Cloud")
        workspace = user.id
        token = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=token, user_id=user.id,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(account_id=workspace, owner_id=user.id)
        db.add(conversation)
        settings = db.get(CloudComputerSettings, 1)
        if settings is None:
            settings = CloudComputerSettings(id=1, version=1)
            db.add(settings)
        settings.enabled, settings.encrypted_api_key = True, "synthetic"
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake",
            credits_per_minute=".6", oss_role_arn="acs:ram::123:role/test").model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, token)
        conversation_id = conversation.id
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    monkeypatch.setattr("app.storage_backend.active_settings", lambda _: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.storage_backend.current_storage", lambda _: SimpleNamespace(
        bucket="test-bucket", region="cn-hangzhou"))
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    monkeypatch.setattr("app.shared_resources.backend", lambda _: store)
    return access, conversation_id, fake


def test_concurrent_create_and_command_replay(auth_postgres, monkeypatch):
    engine = auth_postgres
    access, conversation, fake = seed(engine, monkeypatch)
    barrier, request = Barrier(4), uuid4()
    def create(_):
        with Session(engine) as db:
            barrier.wait(10)
            return cloud.create_computer(db, access, conversation, request, {"name": "相同创建"})["computer_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(create, range(4)))
    assert len(set(identifiers)) == fake.creates == 1
    identifier = identifiers[0]
    barrier, request = Barrier(4), uuid4()
    def submit(_):
        with Session(engine) as db:
            barrier.wait(10)
            return cloud.submit(db, access, conversation, request,
                {"computer_id": identifier, "command": "echo synthetic"}).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        commands = list(pool.map(submit, range(4)))
    assert len(set(commands)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: cloud.run(commands[0], sessionmaker(bind=engine)), range(4)))
    assert fake.starts == 1
    with Session(engine) as db:
        row = db.get(cloud.Computer, UUID(identifier))
        row.started_at = row.last_activity_at = auth.now() - timedelta(seconds=61)
        db.commit()
        cloud.end(row.id, db, access)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: cloud.maintain_one(UUID(identifier), sessionmaker(bind=engine)), range(4)))
    with Session(engine) as db:
        row = db.get(cloud.Computer, UUID(identifier))
        assert row.ended_at and row.held_credits == 0
        assert db.get(UserCreditAccount, access.user_id).lifetime_spent == row.charged_credits
        assert fake.kills == 1


def test_independent_provider_creates_do_not_hold_account_lock(auth_postgres, monkeypatch):
    engine = auth_postgres
    access, conversation, fake = seed(engine, monkeypatch)
    barrier = Barrier(4)
    original = fake.create
    def create_provider(identifier):
        barrier.wait(10)  # Fails if a global/account lock spans the provider call.
        return original(identifier)
    monkeypatch.setattr(fake, "create", create_provider)
    def create(index):
        with Session(engine) as db:
            return cloud.create_computer(db, access, conversation, uuid4(), {"name": f"任务{index}"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(create, range(4)))
    assert len({item["computer_id"] for item in receipts}) == fake.creates == 4
    assert all(item["cloud_computer"]["status"] == "ready" for item in receipts)
    with Session(engine) as db:
        held = sum(row.held_credits for row in db.scalars(select(cloud.Computer).where(cloud.Computer.user_id == access.user_id)))
        assert held == Decimal("24")
        account = db.get(UserCreditAccount, access.user_id)
        assert account.balance == account.lifetime_granted - held


def test_same_instance_commands_run_concurrently_and_cancel_independently(auth_postgres, monkeypatch):
    engine = auth_postgres
    access, conversation, fake = seed(engine, monkeypatch)
    with Session(engine) as db:
        identifier = cloud.create_computer(db, access, conversation, uuid4(), {"name": "并行命令"})["computer_id"]
        jobs = [cloud.submit(db, access, conversation, uuid4(),
            {"computer_id": identifier, "command": f"echo {index}"}).id for index in range(2)]
    barrier, original = Barrier(2), fake.start
    def start(*args):
        barrier.wait(10)
        return original(*args)
    monkeypatch.setattr(fake, "start", start)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda job: cloud.run(job, sessionmaker(bind=engine)), jobs))
    with Session(engine) as db:
        assert all(db.get(cloud.Command, job).status == "completed" for job in jobs)
        assert db.get(cloud.Computer, UUID(identifier)).status == "ready"
    assert fake.starts == 2


def test_migration_rejects_destructive_downgrade_and_keeps_rows(legacy_database):
    db, _ = legacy_database
    user_id = account(db, f"legacy-explicit-{uuid4().hex}")
    account_id = workspace(db, "Legacy explicit computers", user_id)
    conversation_id = insert(db, "conversations", workspace_id=account_id, owner_id=user_id,
        title="Legacy", next_sequence=1)
    identifiers = {legacy_cloud(db, account_id, user_id, conversation_id, status="ready") for _ in range(2)}
    ledger_id = insert(db, "credit_ledger", workspace_id=account_id, amount=-20, balance_after=980,
        kind="usage_settlement", idempotency_key=f"legacy-explicit:{user_id}", metadata={})
    revision = migration("0076_explicit_cloud_computers")
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError, match="多台活动云电脑"):
            revision.downgrade()
        db.execute(text("UPDATE cloud_computer_sessions SET status='ended', ended_at=now() "
                        "WHERE user_id=:user_id"), {"user_id": user_id})
        revision.downgrade()
        revision.upgrade()
    assert set(db.scalars(text("SELECT id FROM cloud_computer_sessions WHERE user_id=:user_id"),
        {"user_id": user_id})) == identifiers
    assert db.scalar(text("SELECT amount FROM credit_ledger WHERE id=:id"), {"id": ledger_id}) == -20
