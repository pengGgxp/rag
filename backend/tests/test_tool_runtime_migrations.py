"""Exercise tool DDL and published upgrade paths only on disposable PostgreSQL."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import (
    ChatMessage, CloudComputerSession, Conversation, CreditAccount, CreditLedgerEntry,
    RemoteToolExecution, User, Workspace,
)

from test_auth_postgres import auth_postgres
from test_knowledge_mentions_migration import migration_scripts


def migration(name):
    path = Path(__file__).parents[1] / "alembic" / "versions" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def schema(auth_postgres):
    with auth_postgres.connect() as connection:
        transaction = connection.begin()
        name = "tool_runtime_" + uuid4().hex
        connection.execute(sa.text(f'CREATE SCHEMA "{name}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{name}"'))
        try:
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
        finally:
            transaction.rollback()


def test_invocation_migration_preserves_legacy_rows_and_rejects_lossy_downgrade(schema):
    metadata = sa.MetaData()
    table = sa.Table("remote_tool_executions", metadata,
        sa.Column("id", sa.UUID, primary_key=True), sa.Column("message_id", sa.UUID),
        sa.Column("tool_name", sa.String), sa.Column("arguments_digest", sa.String),
        sa.Column("encrypted_result", sa.Text),
        sa.UniqueConstraint("message_id", "tool_name", "arguments_digest"))
    metadata.create_all(schema)
    old_id, message = uuid4(), uuid4()
    schema.execute(table.insert().values(id=old_id, message_id=message, tool_name="mcp_read",
        arguments_digest="same", encrypted_result="historical-encrypted-result"))
    module = migration("0074_remote_tool_invocation")
    module.upgrade()
    assert schema.execute(sa.text("SELECT id, invocation_id, encrypted_result FROM remote_tool_executions")).one() == (
        old_id, None, "historical-encrypted-result")
    module.downgrade()
    module.upgrade()
    for _ in range(2):
        schema.execute(sa.text("INSERT INTO remote_tool_executions "
            "(id, invocation_id, message_id, tool_name, arguments_digest) VALUES (:id,:invocation,:message,'mcp_read','same')"),
            {"id": uuid4(), "invocation": uuid4(), "message": message})
    before = schema.execute(sa.text("SELECT * FROM remote_tool_executions ORDER BY id")).all()
    with pytest.raises(RuntimeError, match="preserve receipts"):
        module.downgrade()
    assert schema.execute(sa.text("SELECT * FROM remote_tool_executions ORDER BY id")).all() == before
    assert len(before) == 3


def test_call_cap_migration_preserves_prices_and_configuration(schema):
    schema.execute(sa.text("CREATE TABLE site_search_settings "
        "(id integer PRIMARY KEY, max_search_calls integer NOT NULL, max_fetch_calls integer NOT NULL, prices jsonb)"))
    schema.execute(sa.text("""INSERT INTO site_search_settings VALUES (1,3,5,'{"price":"0.1234"}')"""))
    module = migration("0073_remove_tool_call_caps")
    module.upgrade()
    assert schema.execute(sa.text("SELECT * FROM site_search_settings")).one() == (1, {"price": "0.1234"})
    module.downgrade()
    assert schema.execute(sa.text("SELECT max_search_calls,max_fetch_calls,prices FROM site_search_settings")).one() == (
        3, 5, {"price": "0.1234"})


@pytest.mark.parametrize("starting_revision", [
    "base", "0073_cloud_stop_reason", "0074_remote_tool_invocation",
])
def test_combined_head_preserves_each_published_branch(starting_revision):
    configured = os.environ.get("AUTH_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("requires AUTH_TEST_DATABASE_URL for disposable PostgreSQL")
    url = make_url(configured)
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith("rag_auth_test_"):
        pytest.fail("refusing non-test database")
    name = f"rag_auth_test_tool_merge_{uuid4().hex[:10]}"
    admin = sa.create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database_url, "MIGRATION_DATABASE_URL": database_url}
    engine = sa.create_engine(database_url)
    root = Path(__file__).resolve().parents[1]

    def upgrade(target):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target],
            cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr

    def snapshot(tables):
        with engine.connect() as connection:
            return {table.name: connection.execute(sa.select(*(
                column for column in table.c if column.name not in {
                    "max_search_calls", "max_fetch_calls"})).order_by(
                *table.primary_key)).mappings().all() for table in tables}

    try:
        upgrade(starting_revision)
        tables = []
        if starting_revision != "base":
            with Session(engine) as db:
                workspace = Workspace(name="Synthetic combined migration")
                user = User(email=f"tool-merge-{uuid4().hex}@example.com", name="Migration test")
                db.add_all([workspace, user])
                db.flush()
                conversation = Conversation(workspace_id=workspace.id, owner_id=user.id)
                db.add(conversation)
                db.flush()
                message = ChatMessage(conversation_id=conversation.id, sequence=0,
                    role="assistant", content="Preserved answer", status="completed")
                db.add(message)
                db.add(CreditAccount(workspace_id=workspace.id, balance="18.2345"))
                db.add(CreditLedgerEntry(workspace_id=workspace.id, actor_user_id=user.id,
                    amount="18.2345", balance_after="18.2345", kind="grant",
                    idempotency_key=f"merge-{uuid4()}"))
                db.flush()
                cloud = dict(workspace_id=workspace.id, user_id=user.id,
                    conversation_id=conversation.id, session_hash=uuid4().hex,
                    status="ended", snapshot={"credits_per_minute": "0.1234"},
                    encrypted_api_key="synthetic", held_credits=0, charged_credits="0.1234")
                receipt = dict(message_id=message.id, workspace_id=workspace.id,
                    owner_id=user.id, request_id=uuid4(), tool_name="mcp_read",
                    authorization_version=1, arguments_digest="same", status="completed",
                    encrypted_result="preserved-encrypted-result")
                if starting_revision == "0073_cloud_stop_reason":
                    cloud["stop_reason"] = "user_requested"
                else:
                    receipt["invocation_id"] = uuid4()
                db.execute(CloudComputerSession.__table__.insert().values(**cloud))
                db.execute(RemoteToolExecution.__table__.insert().values(**receipt))
                db.commit()
            with engine.begin() as connection:
                settings = sa.Table("site_search_settings", sa.MetaData(), autoload_with=connection)
                values = dict(id=1, version=7, enabled=True, timeout_seconds=20,
                    result_count=5, prices={"search": "0.1234"})
                if "max_search_calls" in settings.c:
                    values.update(max_search_calls=3, max_fetch_calls=5)
                connection.execute(settings.insert().values(**values))
            metadata = sa.MetaData()
            tables = [sa.Table(table, metadata, autoload_with=engine, resolve_fks=False)
                for table in ("credit_accounts", "credit_ledger", "conversations", "chat_messages",
                    "cloud_computer_sessions", "remote_tool_executions", "site_search_settings")]
        before = snapshot(tables)
        upgrade("head")
        upgrade("head")
        assert snapshot(tables) == before
        with engine.connect() as connection:
            assert list(connection.scalars(sa.text("SELECT version_num FROM alembic_version"))) == [
                migration_scripts().get_current_head()]
            inspector = sa.inspect(connection)
            cloud_columns = {column["name"]: column for column in inspector.get_columns(
                "cloud_computer_sessions")}
            receipt_columns = {column["name"]: column for column in inspector.get_columns(
                "remote_tool_executions")}
            assert cloud_columns["stop_reason"]["nullable"]
            assert receipt_columns["invocation_id"]["nullable"]
            assert any(constraint["column_names"] == ["invocation_id"] for constraint in
                inspector.get_unique_constraints("remote_tool_executions"))
            assert not any(constraint["column_names"] == [
                "message_id", "tool_name", "arguments_digest"] for constraint in
                inspector.get_unique_constraints("remote_tool_executions"))
            assert not {"max_search_calls", "max_fetch_calls"} & {
                column["name"] for column in inspector.get_columns("site_search_settings")}
            if starting_revision != "base":
                assert connection.scalar(sa.text("SELECT stop_reason FROM cloud_computer_sessions")) == (
                    "user_requested" if starting_revision == "0073_cloud_stop_reason" else None)
                if starting_revision == "0073_cloud_stop_reason":
                    assert connection.scalar(sa.text("SELECT invocation_id FROM remote_tool_executions")) is None
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE "{name}"'))
        admin.dispose()
