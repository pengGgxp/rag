"""Exercise new DDL in a rolled-back schema on the disposable PostgreSQL database."""
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from test_auth_postgres import auth_postgres


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
