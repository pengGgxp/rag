"""Preview uniqueness is checked only on an explicitly disposable database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth
from app.models import CloudComputerSession, CloudComputerCommand, CloudPreview, Conversation
from test_auth_postgres import auth_postgres


def test_one_active_preview_and_release(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user, workspace_id = auth.create_user(db, f"preview-{uuid4().hex}@example.com", "Preview test")
        conversation = Conversation(workspace_id=workspace_id, owner_id=user.id)
        db.add(conversation); db.flush()
        computer = CloudComputerSession(workspace_id=workspace_id, user_id=user.id, conversation_id=conversation.id,
            session_hash="synthetic", snapshot={}, encrypted_api_key="unused", status="ready")
        db.add(computer); db.flush()
        commands = [CloudComputerCommand(session_id=computer.id, request_id=uuid4(), digest=uuid4().hex, payload={}) for _ in range(2)]
        db.add_all(commands); db.commit()
        computer_id, command_ids = computer.id, [command.id for command in commands]
    barrier = Barrier(2)
    def create(command_id):
        with Session(engine) as db:
            row = CloudPreview(session_id=computer_id, command_id=command_id, port=8080, name="synthetic", status="starting")
            db.add(row); barrier.wait(timeout=10)
            try:
                db.commit(); return True
            except IntegrityError:
                db.rollback(); return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(create, command_ids))
    assert sum(result) == 1
    with Session(engine) as db:
        row = db.scalar(select(CloudPreview).where(CloudPreview.session_id == computer_id))
        unused = next(identifier for identifier in command_ids if identifier != row.command_id)
        row.status, row.ended_at = "stopped", auth.now()
        db.flush()
        db.add(CloudPreview(session_id=computer_id, command_id=unused, port=8080, name="replacement", status="starting"))
        db.commit()
