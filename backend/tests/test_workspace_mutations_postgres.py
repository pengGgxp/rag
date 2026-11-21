"""Real PostgreSQL concurrency under the platform write lock, local bytes only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, shared_resources, workspace_mutations as mutations
from app.models import WorkspaceFileOperation
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


@pytest.mark.parametrize("directory,neighbor", [("a_b", "axb"), ("a%b", "axxb"), ("a/b", "axb")])
def test_literal_directory_queries_and_rename_replay(auth_postgres, monkeypatch, directory, neighbor):
    from uuid import UUID
    from app import personal_skills, resources
    store = MemoryMount()
    monkeypatch.setattr(shared_resources, "backend", lambda db: store)
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"review-{uuid4().hex}@example.com", "临时复测", "test-password-123")
        db.commit()
        actor = auth.Access(user.id, "")
        manifests = {}
        for name in (directory, neighbor):
            root = f".skills/{name}"
            result = mutations.save_files(db, actor, {
                f"{root}/SKILL.md": (b"---\nname: review\n---\nbody", "text/markdown"),
                f"{root}/inside.txt": (b"original", "text/plain")})
            manifests[name] = result["items"][0]
            mutations.create_directory(db, actor, f"{root}/empty")
        root = f".skills/{directory}"
        items = resources._children(db, actor, root, "", "name", "asc", "")
        assert {item["path"] for item in items} == {f"{root}/SKILL.md", f"{root}/inside.txt", f"{root}/empty"}
        files = personal_skills.files(UUID(manifests[directory]["id"]), db, actor)
        assert {item["path"] for item in files} == {f"{root}/SKILL.md", f"{root}/inside.txt"}
        item = next(item for item in files if item["path"].endswith("inside.txt"))
        request = resources.PathPayload(new_path=f"{root}/after.txt", revision=item["revision"], request_id=str(uuid4()))
        first = resources.rename_file(UUID(item["id"]), request, db, actor)
        assert first["path"] == f"{root}/after.txt"
        # A new DB session models the next worker after the first response was lost.
    with Session(auth_postgres) as db:
        assert resources.rename_file(UUID(item["id"]), request, db, actor) == first


def test_concurrent_writes_and_request_id_are_serialized(auth_postgres, monkeypatch):
    engine = auth_postgres
    store = MemoryMount()
    monkeypatch.setattr(shared_resources, "backend", lambda db: store)
    with Session(engine) as db:
        user = auth.create_user(db, f"files-{uuid4().hex}@example.com", "临时并发验收", "test-password-123")
        db.commit()
        actor = auth.Access(user.id, "")
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
            WorkspaceFileOperation.account_id == actor.user_id, WorkspaceFileOperation.request_id == request_id)).all()
        assert len(records) == 1 and records[0].state == "completed"


def test_concurrent_file_rename_replays_one_committed_response(auth_postgres, monkeypatch):
    from uuid import UUID
    from app import resources
    store = MemoryMount()
    monkeypatch.setattr(shared_resources, "backend", lambda db: store)
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"rename-{uuid4().hex}@example.com", "临时改名并发验收", "test-password-123")
        db.commit()
        actor = auth.Access(user.id, "")
        item = mutations.save_files(db, actor, {"before.txt": (b"original", "text/plain")})["items"][0]
    request = resources.PathPayload(new_path="after.txt", revision=item["revision"], request_id=str(uuid4()))
    barrier = Barrier(2)
    def rename(_):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            return resources.rename_file(UUID(item["id"]), request, db, actor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(rename, range(2)))
    assert values[0] == values[1] and values[0]["path"] == "after.txt"
    with Session(auth_postgres) as db:
        records = db.scalars(select(WorkspaceFileOperation).where(
            WorkspaceFileOperation.account_id == actor.user_id, WorkspaceFileOperation.request_id == request.request_id)).all()
        assert len(records) == 1 and records[0].state == "completed"
