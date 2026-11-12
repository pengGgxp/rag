"""Revision length and data preservation for message knowledge mentions."""
import os
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import Conversation, CreditAccount, CreditLedgerEntry, User, Workspace


ROOT = Path(__file__).resolve().parents[1]


def test_revisions_fit_postgres_version_column():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    limit = MigrationContext.configure(dialect_name="postgresql")._version.c.version_num.type.length
    assert len(scripts.get_heads()) == 1
    assert all(len(revision.revision) <= limit for revision in scripts.walk_revisions())


def test_postgres_upgrade_preserves_messages_and_billing():
    configured = os.environ.get("AUTH_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("requires AUTH_TEST_DATABASE_URL for disposable PostgreSQL")
    url = make_url(configured)
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("rag_auth_test_"):
        pytest.fail("refusing non-test database")
    name = f"rag_auth_test_mentions_{uuid4().hex[:10]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database_url, "MIGRATION_DATABASE_URL": database_url}
    engine = create_engine(database_url)

    def migrate(direction, target):
        subprocess.run([sys.executable, "-m", "alembic", direction, target], cwd=ROOT, env=env,
            capture_output=True, check=True, timeout=60)

    def tables():
        metadata = MetaData()
        return {name: Table(name, metadata, autoload_with=engine, resolve_fks=False) for name in (
            "conversations", "chat_messages", "credit_accounts", "credit_ledger")}

    def snapshot(schema):
        with engine.connect() as connection:
            return {name: list(connection.execute(select(table).order_by(*table.primary_key)).mappings())
                for name, table in schema.items()}

    try:
        migrate("upgrade", "0063_deployment_maintenance")
        schema = tables()
        conversation_id, message_id = uuid4(), uuid4()
        with Session(engine) as db:
            workspace = Workspace(name="Synthetic migration workspace")
            user = User(email=f"mentions-{uuid4().hex}@example.com", name="Migration test")
            db.add_all([workspace, user]); db.flush()
            db.add(Conversation(id=conversation_id, workspace_id=workspace.id, owner_id=user.id,
                title="Preserved conversation"))
            db.add(CreditAccount(workspace_id=workspace.id, balance=Decimal("18.2345")))
            db.add(CreditLedgerEntry(workspace_id=workspace.id, actor_user_id=user.id,
                amount=Decimal("18.2345"), balance_after=Decimal("18.2345"), kind="grant",
                idempotency_key=f"mentions-{uuid4()}"))
            db.flush()
            db.execute(schema["chat_messages"].insert().values(id=message_id,
                conversation_id=conversation_id, sequence=0, role="user", content="Preserved message",
                status="completed", citations=[{"chunk_id": str(uuid4()), "text": "Historical evidence"}],
                traces=[{"id": str(uuid4())}], document_mentions=[{"id": str(uuid4())}],
                tool_events=[], refused=False))
            db.commit()
        before = snapshot(schema)
        migrate("upgrade", "head")
        migrate("upgrade", "head")
        assert snapshot(schema) == before
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            length = next(column["type"].length for column in inspect(connection).get_columns(
                "alembic_version") if column["name"] == "version_num")
            assert version == "0066_context_windows" and len(version) <= length == 32
            assert connection.scalar(text("SELECT knowledge_base_mentions FROM chat_messages")) == []
            assert connection.scalar(text("SELECT resource_mentions FROM chat_messages")) == []
        current_messages = tables()["chat_messages"]
        with engine.begin() as connection:
            connection.execute(current_messages.insert().values(id=uuid4(), conversation_id=conversation_id,
                sequence=1, role="assistant", content="New message", status="completed",
                citations=[], traces=[], tool_events=[], refused=False))
            assert list(connection.scalars(select(current_messages.c.knowledge_base_mentions))) == [[], []]
            assert list(connection.scalars(select(current_messages.c.resource_mentions))) == [[], []]
            value = [{"id": str(uuid4()), "filename": "sample.txt", "path": "data/sample.txt"}]
            connection.execute(current_messages.update().where(current_messages.c.id != message_id)
                .values(resource_mentions=value))
            assert connection.scalar(select(current_messages.c.resource_mentions).where(
                current_messages.c.id != message_id)) == value
            connection.execute(current_messages.delete().where(current_messages.c.id != message_id))
        migrate("downgrade", "0063_deployment_maintenance")
        assert snapshot(schema) == before
        migrate("upgrade", "head")
        assert snapshot(schema) == before
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
