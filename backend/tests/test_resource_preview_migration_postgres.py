"""The historical preview-cache migration remains reversible and data-safe."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text


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
            migration.downgrade()
            assert connection.scalar(text("SELECT path FROM workspace_files")) == "original.docx"
            assert float(connection.scalar(text("SELECT amount FROM ledger"))) == 42.125
            migration.upgrade()
        finally:
            connection.execute(text("SET LOCAL search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
