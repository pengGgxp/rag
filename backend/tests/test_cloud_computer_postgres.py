"""Run only against an explicitly named, disposable migrated PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, cloud_computer as cloud
from app.models import CloudComputerSettings, CloudComputerSession, CloudComputerCommand, Conversation, LoginSession
from test_auth_postgres import auth_postgres


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
        settings.config = cloud.Configuration(region="cn-hangzhou", template="fake", specification="fake", credits_per_minute=0).model_dump(mode="json")
        db.commit()
        access = auth.Access(user.id, workspace_id, "admin", session_hash)
        conversation_id = conversation.id
    monkeypatch.setattr(cloud, "dispatch", lambda *_: None)
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
