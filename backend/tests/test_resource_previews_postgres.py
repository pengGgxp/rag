"""Opt-in preview migration and concurrent creation on a disposable database."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, resource_previews as preview
from app.models import ResourcePreview, User, Workspace, WorkspaceFile
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def test_preview_migration_preserves_resources_and_ledger(auth_postgres):
    schema = "preview_probe_" + uuid4().hex
    with auth_postgres.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE workspace_files (id uuid PRIMARY KEY, path text)"))
            connection.execute(text("CREATE TABLE ledger (amount numeric)"))
            resource = uuid4()
            connection.execute(text("INSERT INTO workspace_files VALUES (:id,'original.docx')"), {"id": resource})
            connection.execute(text("INSERT INTO ledger VALUES (42.125)"))
            location = Path(__file__).parents[1] / "alembic/versions/0061_resource_previews.py"
            spec = importlib.util.spec_from_file_location("preview_migration_probe", location)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            insert = text("""INSERT INTO resource_previews (id,workspace_id,resource_id,revision,kind,status)
                VALUES (:id,:wid,:rid,'revision','thumbnail','queued')""")
            values = {"id": uuid4(), "wid": uuid4(), "rid": resource}
            connection.execute(insert, values)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(insert, {**values, "id": uuid4()})
            assert connection.scalar(text("SELECT count(*) FROM resource_previews")) == 1
            migration.downgrade()
            assert connection.scalar(text("SELECT path FROM workspace_files")) == "original.docx"
            assert float(connection.scalar(text("SELECT amount FROM ledger"))) == 42.125
            migration.upgrade()
        finally:
            connection.execute(text("SET LOCAL search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_concurrent_prepare_creates_one_intent(auth_postgres, monkeypatch):
    store = MemoryMount()
    monkeypatch.setattr(preview, "current_storage", lambda *_a, **_k: store)
    monkeypatch.setattr(preview, "dispatch", lambda _: None)
    with Session(auth_postgres) as db:
        workspace = Workspace(name="Preview isolation")
        user = User(email=f"preview-{uuid4()}@example.com", name="Preview test")
        db.add_all([workspace, user]); db.flush()
        row = WorkspaceFile(workspace_id=workspace.id, path="original.png", content_type="image/png",
            size_bytes=4, content_hash="x", created_by=user.id, updated_by=user.id,
            expires_at=auth.now()+timedelta(days=1))
        db.add(row); db.commit()
        identifier = row.id
        access = auth.Access(user.id, workspace.id, "admin", "test")
        store.objects[preview.object_key(workspace.id, row.path)] = b"test"
    gate = Barrier(4)
    def create(_):
        with Session(auth_postgres, expire_on_commit=False) as db:
            gate.wait(timeout=10)
            return preview.prepare(identifier, preview.Prepare(), db, access)["id"]
    with ThreadPoolExecutor(4) as pool:
        identifiers = list(pool.map(create, range(4)))
    assert len(set(identifiers)) == 1
    with Session(auth_postgres) as db:
        rows = list(db.scalars(select(ResourcePreview).where(ResourcePreview.resource_id == identifier)))
        assert len(rows) == 1 and rows[0].status == "queued"
