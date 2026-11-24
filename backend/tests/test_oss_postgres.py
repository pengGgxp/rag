"""Opt-in migrated PostgreSQL checks; fixture refuses production database names."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.operations import migrate_storage
from app.files import settings as storage_settings
from app.models import SiteStorageSettings, StorageMigration, StorageMigrationObject
from test_auth_postgres import auth_postgres


def test_schema_preservation_unique_active_and_serialized_settings(auth_postgres):
    engine = auth_postgres
    assert "storage_migration_objects" in inspect(engine).get_table_names()
    with Session(engine) as db:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(
            Config("alembic.ini")).get_current_head()
        row = db.get(SiteStorageSettings, 1)
        if not row:
            row = SiteStorageSettings(id=1, provider="cos", bucket="retained-123", region="ap-guangzhou",
                version=7, tested_version=7, active=True, encrypted_secret_id="retained-encrypted-id",
                encrypted_secret_key="retained-encrypted-key")
            db.add(row)
            db.commit()
        before = (row.bucket, row.region, row.encrypted_secret_id, row.encrypted_secret_key)
        db.add(SiteStorageSettings(id=2, provider="oss", bucket="target", region="cn-hangzhou",
            version=1, tested_version=1, active=True))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert (row.bucket, row.region, row.encrypted_secret_id, row.encrypted_secret_key) == before
        original_version = row.version
    barrier = Barrier(2)
    def update(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            row = storage_settings.locked(db, "cos")
            if row.version != original_version:
                return "conflict"
            row.version += 1
            db.commit()
            return "saved"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == ["conflict", "saved"]
    with Session(engine) as db:
        assert db.get(SiteStorageSettings, 1).version == original_version + 1
        assert (db.get(SiteStorageSettings, 1).bucket, db.get(SiteStorageSettings, 1).region,
            db.get(SiteStorageSettings, 1).encrypted_secret_id, db.get(SiteStorageSettings, 1).encrypted_secret_key) == before
    # Dedicated connection keeps the session lock while per-object commits run.
    with engine.connect() as connection, Session(bind=connection) as db:
        with migrate_storage.migration_lock(db):
            db.commit()
            with engine.connect() as other:
                assert not other.scalar(text("SELECT pg_try_advisory_lock(3020046)"))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_try_advisory_lock(3020046)"))
        connection.execute(text("SELECT pg_advisory_unlock(3020046)"))
