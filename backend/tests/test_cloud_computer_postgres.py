"""Run only against an explicitly named, disposable migrated PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app import auth, cloud_computer as cloud
from app.models import CloudComputerSettings, CloudComputerSession, CloudComputerCommand, Conversation, LoginSession
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud


def test_concurrent_cloud_admission_and_replay(auth_postgres, monkeypatch):
    engine = auth_postgres
    with Session(engine) as db:
        user, workspace_id = auth.create_user(db, f"cloud-{uuid4().hex}@example.com", "Cloud test")
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id, workspace_id=workspace_id,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(workspace_id=workspace_id, owner_id=user.id)
        db.add(conversation)
        settings = db.get(CloudComputerSettings, 1)
        if not settings:
            settings = CloudComputerSettings(id=1, version=1)
            db.add(settings)
        settings.enabled = True
        settings.encrypted_api_key = "test-only-not-used"
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake", credits_per_minute=0,
            oss_role_arn="acs:ram::123:role/test").model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, workspace_id, "admin", session_hash)
        conversation_id = conversation.id
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    monkeypatch.setattr("app.storage_backend.active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: None)
    barrier = Barrier(4)
    request_id = uuid4()
    def submit(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return cloud.submit(db, access, conversation_id, request_id, {"command": "echo synthetic"}).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(4)))
    assert len(set(ids)) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CloudComputerSession).where(CloudComputerSession.user_id == access.user_id)) == 1
        assert db.scalar(select(func.count()).select_from(CloudComputerCommand).where(CloudComputerCommand.id == ids[0])) == 1
        row = db.scalar(select(CloudComputerSession).where(CloudComputerSession.user_id == access.user_id))
        row.status, row.ended_at = "ended", auth.now()
        command = db.get(CloudComputerCommand, ids[0])
        command.status = "completed"
        db.commit()
        assert cloud.submit(db, access, conversation_id, request_id, {"command": "echo synthetic"}).id == ids[0]


def test_tool_call_concurrency_worker_restart_and_billing(auth_postgres, monkeypatch):
    engine = auth_postgres
    with Session(engine) as db:
        user, workspace_id = auth.create_user(db, f"cloud-call-{uuid4().hex}@example.com", "Cloud call test")
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id, workspace_id=workspace_id,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(workspace_id=workspace_id, owner_id=user.id)
        db.add(conversation)
        settings = db.get(CloudComputerSettings, 1)
        if settings is None:
            settings = CloudComputerSettings(id=1, version=1)
            db.add(settings)
        settings.enabled, settings.encrypted_api_key = True, "test-only"
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake",
            credits_per_minute="0.6", oss_role_arn="acs:ram::123:role/test").model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, workspace_id, "admin", session_hash)
        conversation_id = conversation.id
    fake = FakeCloud()
    monkeypatch.setattr("app.shared_resources.backend", lambda _: None)
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    monkeypatch.setattr("app.storage_backend.active_settings", lambda _: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.storage_backend.current_storage", lambda _: SimpleNamespace(
        bucket="test-bucket", region="cn-hangzhou"))
    request_id = uuid4()
    barrier = Barrier(4)
    def submit_duplicate(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
                {"command": "echo synthetic"}).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit_duplicate, range(4)))
    assert len(set(ids)) == 1
    with Session(engine) as db:
        from app.models import CreditAccount, CreditLedgerEntry, UsageEvent
        held_entries = [row.id for row in db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == workspace_id))]
        assert db.scalar(select(func.count()).select_from(CloudComputerSession).where(
            CloudComputerSession.user_id == access.user_id)) == 1
    # Duplicate deliveries to replacement Workers only execute the durable command once.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: cloud.run(ids[0], sessionmaker(bind=engine)), range(4)))
    assert fake.creates == fake.starts == 1
    barrier = Barrier(4)
    def conflicting_parameters(index):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                return cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
                    {"command": "echo synthetic" if index == 0 else "echo different"}).id
            except HTTPException as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(conflicting_parameters, range(4)))
    assert outcomes.count(409) == 3 and outcomes[0] == ids[0]
    with Session(engine) as db:
        assert [row.id for row in db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == workspace_id))] == held_entries
        second = cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-2-same"),
            {"command": "echo synthetic"})
        second_id = second.id
        assert second_id != ids[0]
    cloud.run(second_id, sessionmaker(bind=engine))
    assert fake.creates == 1 and fake.starts == 2
    with Session(engine) as db:
        computer = db.scalar(select(CloudComputerSession).where(CloudComputerSession.user_id == access.user_id))
        computer.started_at = computer.last_activity_at = auth.now()-timedelta(seconds=61)
        computer.status, computer.billing_stopped_at = "stopping", auth.now()
        computer_id = computer.id
        db.commit()
    cloud.maintain_one(computer_id, sessionmaker(bind=engine))
    with Session(engine) as db:
        computer = db.get(CloudComputerSession, computer_id)
        assert computer.ended_at and computer.held_credits == 0 and computer.charged_credits > 0
        before = (db.get(CreditAccount, workspace_id).balance,
            [row.id for row in db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.workspace_id == workspace_id))],
            [row.id for row in db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace_id))])
        assert cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
            {"command": "echo synthetic"}).id == ids[0]
        assert cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-2-same"),
            {"command": "echo synthetic"}).id == second_id
    cloud.run(ids[0], sessionmaker(bind=engine))
    cloud.maintain_one(computer_id, sessionmaker(bind=engine))
    with Session(engine) as db:
        assert (db.get(CreditAccount, workspace_id).balance,
            [row.id for row in db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.workspace_id == workspace_id))],
            [row.id for row in db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace_id))]) == before
    assert fake.creates == fake.kills == 1 and fake.starts == 2
