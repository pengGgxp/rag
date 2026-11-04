"""Migration probe in a disposable schema, never against production data."""
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from test_auth_postgres import auth_postgres


def test_resource_migration_preserves_objects_messages_and_billing(auth_postgres):
    schema = "resource_probe_" + uuid4().hex
    with auth_postgres.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            definitions = [
                "CREATE TABLE workspaces (id uuid PRIMARY KEY)",
                "CREATE TABLE users (id uuid PRIMARY KEY)",
                "CREATE TABLE conversations (id uuid PRIMARY KEY)",
                "CREATE TABLE chat_messages (id uuid PRIMARY KEY, conversation_id uuid REFERENCES conversations)",
                """CREATE TABLE chat_attachments (id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES workspaces,
                    user_id uuid NOT NULL REFERENCES users, message_id uuid REFERENCES chat_messages ON DELETE CASCADE,
                    filename varchar(255), content_type varchar(100), size_bytes integer, content_hash varchar(64),
                    object_key varchar(500) UNIQUE, kind varchar(20), position integer NOT NULL DEFAULT 0,
                    text_content text, width integer, height integer, deleted_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
                """CREATE TABLE workspace_files (id uuid PRIMARY KEY, workspace_id uuid REFERENCES workspaces,
                    path varchar(512), content_type varchar(150), size_bytes integer, content_hash varchar(64),
                    current_version integer, created_by uuid REFERENCES users, updated_by uuid REFERENCES users,
                    expires_at timestamptz, deleted_at timestamptz, created_at timestamptz, updated_at timestamptz,
                    UNIQUE(workspace_id,path))""",
                """CREATE TABLE workspace_file_versions (id uuid PRIMARY KEY, file_id uuid REFERENCES workspace_files,
                    workspace_id uuid REFERENCES workspaces, version integer, object_key varchar(700) UNIQUE,
                    content_type varchar(150), size_bytes integer, content_hash varchar(64), created_by uuid REFERENCES users,
                    created_at timestamptz DEFAULT CURRENT_TIMESTAMP, deleted_at timestamptz, UNIQUE(file_id,version))""",
                "CREATE TABLE workspace_storage_states (workspace_id uuid PRIMARY KEY, over_quota_since timestamptz)",
                "CREATE TABLE credit_ledger (id integer PRIMARY KEY, amount numeric)",
            ]
            for definition in definitions:
                connection.execute(text(definition))
            wid, uid, cid, mid, aid = [uuid4() for _ in range(5)]
            for table, identifier in [("workspaces", wid), ("users", uid), ("conversations", cid)]:
                connection.execute(text(f"INSERT INTO {table} VALUES (:id)"), {"id": identifier})
            connection.execute(text("INSERT INTO chat_messages VALUES (:id,:cid)"), {"id": mid, "cid": cid})
            connection.execute(text("""INSERT INTO chat_attachments
                (id,workspace_id,user_id,message_id,filename,content_type,size_bytes,content_hash,object_key,kind,created_at)
                VALUES (:id,:wid,:uid,:mid,'same.txt','text/plain',4,:hash,'old/object/key','document','2025-01-01')"""),
                {"id": aid, "wid": wid, "uid": uid, "mid": mid, "hash": "a"*64})
            # A model could already have created precisely the generated migration path.
            connection.execute(text("""INSERT INTO workspace_files (id,workspace_id,path)
                VALUES (:id,:wid,:path)"""), {"id": uuid4(), "wid": wid, "path": f"uploads/{aid}/same.txt"})
            connection.execute(text("INSERT INTO workspace_storage_states VALUES (:wid,'2025-01-01')"), {"wid": wid})
            connection.execute(text("INSERT INTO credit_ledger VALUES (1,42.125)"))
            location = Path(__file__).parents[1] / "alembic/versions/0053_resource_library.py"
            spec = importlib.util.spec_from_file_location("resource_migration_probe", location)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            attachment = connection.execute(text("SELECT * FROM chat_attachments WHERE id=:id"), {"id": aid}).mappings().one()
            version = connection.execute(text("SELECT * FROM workspace_file_versions WHERE file_id=:id"), {"id": aid}).mappings().one()
            assert attachment["resource_file_id"] == aid and attachment["resource_version"] == 1
            assert attachment["resource_path"] != f"uploads/{aid}/same.txt"
            assert attachment["created_at"].year == 2025 and attachment["message_id"] is None
            assert version["object_key"] == "old/object/key" and version["available"] is None
            assert connection.scalar(text("SELECT attachment_id FROM chat_message_resources WHERE message_id=:mid"), {"mid": mid}) == aid
            assert connection.scalar(text("SELECT amount FROM credit_ledger WHERE id=1")) == 42.125
            assert connection.scalar(text("SELECT over_quota_since FROM workspace_storage_states")).year >= 2026
            connection.execute(text("DELETE FROM chat_messages WHERE id=:id"), {"id": mid})
            assert connection.scalar(text("SELECT count(*) FROM chat_attachments")) == 1
            assert connection.scalar(text("SELECT count(*) FROM workspace_file_versions")) == 1
        finally:
            connection.execute(text("SET LOCAL search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
