"""Real PostgreSQL concurrency under the platform write lock, local bytes only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, shared_resources, workspace_mutations as mutations
from app.models import WorkspaceFileOperation
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def test_concurrent_writes_and_request_id_are_serialized(auth_postgres, monkeypatch):
    engine = auth_postgres
    store = MemoryMount()
    monkeypatch.setattr(shared_resources, "backend", lambda db: store)
    with Session(engine) as db:
        user, _ = auth.create_user(db, f"files-{uuid4().hex}@example.com", "临时并发验收", "test-password-123")
        db.commit()
        from app.models import Membership
        workspace = db.scalar(select(Membership.workspace_id).where(Membership.user_id == user.id))
        actor = auth.Access(user.id, workspace, "admin", "")
        initial = mutations.save_files(db, actor, {"note.txt": (b"original", "text/plain")})["items"][0]
    barrier = Barrier(2)
    def save(index):
        from fastapi import HTTPException
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                return mutations.save_files(db, actor, {"note.txt": (str(index).encode(), "text/plain")},
                    expected={"note.txt": initial["revision"]})
            except HTTPException as error:
                return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(save, range(2)))
    assert sum(isinstance(item, dict) for item in values) == 1 and values.count(409) == 1
    request_id = str(uuid4())
    barrier = Barrier(2)
    def create(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return mutations.create_directory(db, actor, "same-folder", request_id=request_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(create, range(2)))
    assert values[0] == values[1]
    with Session(engine) as db:
        records = db.scalars(select(WorkspaceFileOperation).where(
            WorkspaceFileOperation.workspace_id == actor.workspace_id, WorkspaceFileOperation.request_id == request_id)).all()
        assert len(records) == 1 and records[0].state == "completed"
