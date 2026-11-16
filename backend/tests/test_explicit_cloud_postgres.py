from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command as migrations
from alembic.config import Config
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session, sessionmaker

from app import auth, cloud_computer as cloud
from app.models import CloudComputerSettings, Conversation, LoginSession, CreditAccount, CreditLedgerEntry
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud


def seed(engine, monkeypatch):
    with Session(engine) as db:
        user, workspace = auth.create_user(db, f"explicit-{uuid4().hex}@example.com", "Cloud")
        token = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=token, user_id=user.id, workspace_id=workspace,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(workspace_id=workspace, owner_id=user.id)
        db.add(conversation)
        settings = db.get(CloudComputerSettings, 1)
        if settings is None:
            settings = CloudComputerSettings(id=1, version=1)
            db.add(settings)
        settings.enabled, settings.encrypted_api_key = True, "synthetic"
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake",
            credits_per_minute=".6", oss_role_arn="acs:ram::123:role/test").model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, workspace, "admin", token)
        conversation_id = conversation.id
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    monkeypatch.setattr("app.storage_backend.active_settings", lambda _: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.storage_backend.current_storage", lambda _: SimpleNamespace(
        bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.shared_resources.backend", lambda _: None)
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
        assert db.get(CreditAccount, access.workspace_id).lifetime_spent == row.charged_credits
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
        account = db.get(CreditAccount, access.workspace_id)
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


def test_migration_rejects_destructive_downgrade_and_keeps_rows(auth_postgres, monkeypatch):
    engine = auth_postgres
    access, conversation, fake = seed(engine, monkeypatch)
    with Session(engine) as db:
        identifiers = [UUID(cloud.create_computer(db, access, conversation, uuid4(), {"name": "迁移保留"})["computer_id"])
            for _ in range(2)]
        before = list(db.scalars(select(CreditLedgerEntry.id).where(CreditLedgerEntry.workspace_id == access.workspace_id)))
    monkeypatch.setenv("MIGRATION_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    config = Config("alembic.ini")
    with pytest.raises(RuntimeError, match="多台活动云电脑"):
        migrations.downgrade(config, "0073_cloud_stop_reason")
    with Session(engine) as db:
        assert all(db.get(cloud.Computer, identifier) for identifier in identifiers)
        assert list(db.scalars(select(CreditLedgerEntry.id).where(CreditLedgerEntry.workspace_id == access.workspace_id))) == before
        # End synthetic records before explicitly testing the old single-instance schema.
        db.execute(text("UPDATE cloud_computer_sessions SET status='ended', ended_at=now() WHERE ended_at IS NULL"))
        db.commit()
    migrations.downgrade(config, "0073_cloud_stop_reason")
    migrations.upgrade(config, "head")
    with Session(engine) as db:
        assert all(db.get(cloud.Computer, identifier) for identifier in identifiers)
        assert list(db.scalars(select(CreditLedgerEntry.id).where(CreditLedgerEntry.workspace_id == access.workspace_id))) == before
        assert "concurrency" not in db.get(CloudComputerSettings, 1).config
