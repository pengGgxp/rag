"""A real, harmless local subprocess keeps the independent task active during UI QA."""
import json
from pathlib import Path
from uuid import UUID, uuid4
from sqlalchemy import select
from app.cloud_computer import runtime as cloud
from app.identity import auth
from app.infra.db import SessionLocal
from app.models import CloudComputerSession, LoginSession
import acceptance_runtime

ids = json.loads(Path("/acceptance/namespace.json").read_text())
with SessionLocal() as db:
    assert db.bind.url.database == "rag_auth_test_issue118_browser"
    row = db.get(CloudComputerSession, UUID(ids["background_id"]))
    login = db.scalar(select(LoginSession).where(LoginSession.user_id == row.user_id))
    access = auth.Access(row.user_id, login.token_hash)
    if row.ended_at:
        created = cloud.create_computer(db, access, row.conversation_id, uuid4(), {"name": "后台资料整理"})
        row = db.get(CloudComputerSession, UUID(created["computer_id"]))
        ids["background_id"] = str(row.id)
        Path("/acceptance/namespace.json").write_text(json.dumps(ids))
    command = cloud.submit(db, access, row.conversation_id, uuid4(),
        {"computer_id": str(row.id), "command": "sleep 1200"})
    print("Independent local command submitted:", command.id)
