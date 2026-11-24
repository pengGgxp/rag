"""Run only against an explicitly named, disposable migrated PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.cloud_computer import runtime as cloud

from app.identity import auth
from app.models import AgentToolExecution, CloudComputerSettings, CloudComputerSession, CloudComputerCommand, Conversation, LoginSession
from test_auth_postgres import auth_postgres
from test_cloud_computer import FakeCloud


def test_concurrent_cloud_admission_and_replay(auth_postgres, monkeypatch):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"cloud-{uuid4().hex}@example.com", "Cloud test")
        account_id = user.id
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(account_id=account_id, owner_id=user.id)
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
        access = auth.Access(user.id, session_hash)
        conversation_id = conversation.id
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
    monkeypatch.setattr("app.infra.storage_backend.active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    monkeypatch.setattr("app.files.shared.backend", lambda _db: store)
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    with Session(engine) as db:
        computer = cloud.create_computer(db, access, conversation_id, uuid4(), {"name": "并发幂等测试"})
        computer_id = computer["computer_id"]
    barrier = Barrier(4)
    request_id = uuid4()
    def submit(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return cloud.submit(db, access, conversation_id, request_id,
                {"computer_id": computer_id, "command": "echo synthetic"}).id
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
        assert cloud.submit(db, access, conversation_id, request_id,
            {"computer_id": computer_id, "command": "echo synthetic"}).id == ids[0]


@pytest.mark.parametrize("inline", [False, True])
def test_tool_call_concurrency_worker_restart_and_billing(auth_postgres, monkeypatch, inline):
    engine = auth_postgres
    request_id = uuid4()
    execution_id, second_execution_id = (uuid4(), uuid4()) if inline else (None, None)
    with Session(engine) as db:
        user = auth.create_user(db, f"cloud-call-{uuid4().hex}@example.com", "Cloud call test")
        account_id = user.id
        session_hash = uuid4().hex + uuid4().hex
        db.add(LoginSession(token_hash=session_hash, user_id=user.id,
            csrf_token=uuid4().hex, expires_at=auth.now()+timedelta(hours=1)))
        conversation = Conversation(account_id=account_id, owner_id=user.id, active_request_id=request_id)
        db.add(conversation)
        db.flush()
        if inline:
            for identifier, call in ((execution_id, "first"), (second_execution_id, "second")):
                db.add(AgentToolExecution(id=identifier, conversation_id=conversation.id,
                    request_id=request_id, model_item_id=uuid4(), call_id=call, name="run_command"))
        settings = db.get(CloudComputerSettings, 1)
        if settings is None:
            settings = CloudComputerSettings(id=1, version=1)
            db.add(settings)
        settings.enabled, settings.encrypted_api_key = True, "test-only"
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake",
            credits_per_minute="0.6", oss_role_arn="acs:ram::123:role/test").model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, session_hash)
        conversation_id = conversation.id
    fake = FakeCloud()
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    monkeypatch.setattr("app.files.shared.backend", lambda _: store)
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    def dispatch(*_):
        assert not inline, "Agent 任务不得二次派发"
    monkeypatch.setattr(cloud, "dispatch", dispatch)
    monkeypatch.setattr("app.infra.storage_backend.active_settings", lambda _: SimpleNamespace(
        provider="oss", bucket="test-bucket", region="cn-hangzhou"))
    monkeypatch.setattr("app.infra.storage_backend.current_storage", lambda _: SimpleNamespace(
        bucket="test-bucket", region="cn-hangzhou"))
    fake = FakeCloud()
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    with Session(engine) as db:
        computer = cloud.create_computer(db, access, conversation_id, uuid4(), {"name": "账务幂等测试"})
        computer_id = computer["computer_id"]
    barrier = Barrier(4)
    def submit_duplicate(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
                {"computer_id": computer_id, "command": "echo synthetic"}, agent_tool_execution_id=execution_id).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit_duplicate, range(4)))
    assert len(set(ids)) == 1
    with Session(engine) as db:
        from app.models import UserCreditAccount, CreditLedgerEntry, UsageEvent
        held_entries = [row.id for row in db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account_id))]
        assert db.scalar(select(func.count()).select_from(CloudComputerSession).where(
            CloudComputerSession.user_id == access.user_id)) == 1
    # Duplicate deliveries to replacement Workers only execute the durable command once.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: cloud.run(ids[0], sessionmaker(bind=engine),
            agent_tool_execution_id=execution_id), range(4)))
    assert fake.creates == fake.starts == 1
    barrier = Barrier(4)
    def conflicting_parameters(index):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                return cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
                    {"computer_id": computer_id, "command": "echo synthetic" if index == 0 else "echo different"},
                    agent_tool_execution_id=execution_id).id
            except HTTPException as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(conflicting_parameters, range(4)))
    assert outcomes.count(409) == 3 and outcomes[0] == ids[0]
    with Session(engine) as db:
        assert [row.id for row in db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account_id))] == held_entries
        second = cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-2-same"),
            {"computer_id": computer_id, "command": "echo synthetic"}, agent_tool_execution_id=second_execution_id)
        second_id = second.id
        assert second_id != ids[0]
    cloud.run(second_id, sessionmaker(bind=engine), agent_tool_execution_id=second_execution_id)
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
        before = (db.get(UserCreditAccount, account_id).balance,
            [row.id for row in db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == account_id))],
            [row.id for row in db.scalars(select(UsageEvent).where(UsageEvent.account_id == account_id))])
        assert cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-1-same"),
            {"computer_id": computer_id, "command": "echo synthetic"}, agent_tool_execution_id=execution_id).id == ids[0]
        assert cloud.submit(db, access, conversation_id, cloud.tool_request_id(request_id, "tool-2-same"),
            {"computer_id": computer_id, "command": "echo synthetic"}, agent_tool_execution_id=second_execution_id).id == second_id
    cloud.run(ids[0], sessionmaker(bind=engine), agent_tool_execution_id=execution_id)
    cloud.maintain_one(computer_id, sessionmaker(bind=engine))
    with Session(engine) as db:
        assert (db.get(UserCreditAccount, account_id).balance,
            [row.id for row in db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == account_id))],
            [row.id for row in db.scalars(select(UsageEvent).where(UsageEvent.account_id == account_id))]) == before
    assert fake.creates == fake.kills == 1 and fake.starts == 2
