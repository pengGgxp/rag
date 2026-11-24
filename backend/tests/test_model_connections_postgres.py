"""Real migration and concurrent writes in disposable databases only."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.model_catalog.connections import lock_connection
from app.models import ModelConnection
from test_auth_postgres import auth_postgres


def test_concurrent_connection_edits_reject_stale_version(auth_postgres):
    with Session(auth_postgres) as db:
        row = ModelConnection(name="Concurrent", base_url="https://example.test/v1", encrypted_api_key="ciphertext")
        db.add(row); db.commit(); identifier = row.id
    barrier = Barrier(2)
    def edit(name):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                row = lock_connection(db, identifier, 1)
                row.name = name
                row.version += 1
                db.commit()
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["first", "second"])) == [200, 409]


def test_upgrade_preserves_models_and_does_not_merge_equal_connections(auth_postgres):
    url = make_url(os.environ["AUTH_TEST_DATABASE_URL"])
    name = f"rag_auth_test_connections_{uuid4().hex[:12]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database_url, "MIGRATION_DATABASE_URL": database_url}
    root = Path(__file__).resolve().parents[1]
    engine = create_engine(env["DATABASE_URL"])
    def migrate(target):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root, env=env,
                       capture_output=True, check=True, timeout=45)
    try:
        migrate("0032_thinking_diagnostics")
        identifiers = [uuid4(), uuid4()]
        with engine.begin() as db:
            for index, identifier in enumerate(identifiers):
                db.execute(text("""INSERT INTO site_model_providers
                    (id,key,name,base_url,model,encrypted_api_key,timeout_seconds,enabled,published,
                     version,thinking_config,thinking_version,prices)
                    VALUES (:id,:key,'Same connection','https://same.example/v1',:model,'opaque-secret',75,
                            :enabled,true,9,'{}',0,:prices)"""),
                    {"id": identifier, "key": f"original-{index}", "model": f"model-{index}",
                     "enabled": index == 0, "prices": '{"llm_input_tokens":{"credits_per_unit":17}}'})
        migrate("head")
        migrate("head")
        with engine.connect() as db:
            rows = db.execute(text("""SELECT m.id,m.version,m.enabled,m.prices,c.id AS connection_id,
                c.platform,c.api_protocol,c.base_url,c.encrypted_api_key,c.timeout_seconds,c.enabled AS connection_enabled
                FROM site_model_providers m JOIN model_connections c ON c.id=m.connection_id ORDER BY m.key""")).mappings().all()
            assert len(rows) == 2 and rows[0]["connection_id"] != rows[1]["connection_id"]
            for index, row in enumerate(rows):
                assert row["id"] == identifiers[index] and row["version"] == 9
                assert row["enabled"] is (index == 0)
                assert row["platform"] == "custom" and row["connection_enabled"]
                assert row["api_protocol"] == "chat_completions"
                assert row["encrypted_api_key"] == "opaque-secret" and row["timeout_seconds"] == 75
                assert row["prices"] == {"llm_input_tokens": {"credits_per_unit": 17}}
            columns = db.execute(text("""SELECT column_name FROM information_schema.columns
                WHERE table_name='site_model_providers'""")).scalars().all()
            assert not {"base_url", "encrypted_api_key", "timeout_seconds"} & set(columns)
    finally:
        engine.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def test_concurrent_bulk_import_is_atomic_and_unique(auth_postgres):
    from app.models import SiteModelProvider
    from app.model_catalog.shared_providers import BatchModels, create_models
    with Session(auth_postgres) as db:
        connection = ModelConnection(name="Bulk", base_url="https://example.test/v1", encrypted_api_key="ciphertext")
        db.add(connection); db.commit(); identifier = connection.id
    payload = BatchModels.model_validate({"connection_version": 1, "models": [
        {"connection_id": str(identifier), "connection_version": 1, "name": model, "model": model,
         "enabled": False, "policies": []} for model in ("first", "second")]})
    barrier = Barrier(2)
    def save(_):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                rows = create_models(identifier, payload, db=db, actor=None)
                return len(rows)
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, range(2))) == [2, 422]
    with Session(auth_postgres) as db:
        rows = db.query(SiteModelProvider).filter_by(connection_id=identifier).all()
        assert sorted(row.model for row in rows) == ["first", "second"]
        assert all(not row.enabled and not row.prices for row in rows)
