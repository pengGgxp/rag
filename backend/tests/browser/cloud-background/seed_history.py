"""Populate only the isolated browser DB; no real provider or production objects."""
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from app import auth, cloud_computer as cloud
from app.db import SessionLocal
from app.models import CloudComputerSession, Conversation, LoginSession
import acceptance_runtime

ids = json.loads(Path("/acceptance/namespace.json").read_text())
with SessionLocal() as db:
    assert db.bind.url.database == "rag_auth_test_issue118_browser"
    user_id, workspace_id = UUID(ids["user_id"]), UUID(ids["workspace_id"])
    login = db.scalar(select(LoginSession).where(LoginSession.user_id == user_id))
    access = auth.Access(user_id, workspace_id, "admin", login.token_hash)
    conversation = Conversation(owner_id=user_id, workspace_id=workspace_id, title="其他任务")
    db.add(conversation); db.flush()
    background = cloud.create_computer(db, access, conversation.id, uuid4(), {"name": "后台资料整理"})
    original = db.get(CloudComputerSession, UUID(background["computer_id"]))
    for i in range(24):
        started = auth.now() - timedelta(days=i + 1)
        db.add(CloudComputerSession(user_id=user_id, workspace_id=workspace_id, conversation_id=conversation.id,
            session_hash=login.token_hash, name=f"历史文件整理 {i + 1}", status="ended",
            snapshot=original.snapshot, encrypted_api_key="synthetic-only",
            started_at=started, ended_at=started + timedelta(seconds=60), billing_stopped_at=started + timedelta(seconds=60),
            last_activity_at=started, stop_reason="user_requested"))
    other, other_workspace = auth.create_user(db, "issue118-other@example.com", "其他临时用户")
    other_conversation = Conversation(owner_id=other.id, workspace_id=other_workspace)
    db.add(other_conversation); db.flush()
    db.add(CloudComputerSession(user_id=other.id, workspace_id=other_workspace, conversation_id=other_conversation.id,
        session_hash="isolated", name="不应出现的其他用户记录", status="ended",
        snapshot=original.snapshot, encrypted_api_key="synthetic-only", ended_at=auth.now(),
        last_activity_at=auth.now()))
    db.commit()
    ids["background_id"] = background["computer_id"]
    Path("/acceptance/namespace.json").write_text(json.dumps(ids))
    print("Added 24 ended histories, an independent active task and a foreign user record to isolated DB.")
