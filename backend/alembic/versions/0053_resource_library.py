"""Persist workspace resources independently of chat lifetime."""
from alembic import op
import sqlalchemy as sa
from uuid import uuid4
from sqlalchemy.dialects.postgresql import UUID

revision = "0053_resource_library"
down_revision = "0052_administrator_contact"
branch_labels = depends_on = None


def upgrade():
    op.create_table("chat_message_resources",
        sa.Column("message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("attachment_id", UUID(as_uuid=True), sa.ForeignKey("chat_attachments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chat_attachments", sa.Column("source", sa.String(24), nullable=False,
        server_default="chat_upload"))
    op.add_column("chat_attachments", sa.Column("conversation_id", UUID(as_uuid=True), nullable=True))
    op.add_column("chat_attachments", sa.Column("resource_path", sa.String(512), nullable=True))
    op.add_column("chat_attachments", sa.Column("over_quota_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_attachments", sa.Column("object_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_chat_attachments_conversation_id", "chat_attachments", ["conversation_id"])
    op.create_foreign_key("fk_chat_attachments_conversation_id", "chat_attachments", "conversations",
        ["conversation_id"], ["id"], ondelete="SET NULL")
    op.execute("""
        UPDATE chat_attachments AS a
        SET conversation_id = c.conversation_id,
            resource_path = 'uploads/' || a.id::text || '/' || a.filename
        FROM chat_messages AS c
        WHERE a.message_id = c.id
    """)
    op.execute("""
        INSERT INTO chat_message_resources (message_id, attachment_id, position)
        SELECT message_id, id, position FROM chat_attachments WHERE message_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)
    op.execute("UPDATE chat_attachments SET message_id = NULL")
    op.drop_constraint("chat_attachments_message_id_fkey", "chat_attachments", type_="foreignkey")
    op.create_foreign_key("chat_attachments_message_id_fkey", "chat_attachments", "chat_messages",
        ["message_id"], ["id"], ondelete="SET NULL")
    op.add_column("workspace_files", sa.Column("source", sa.String(24), nullable=False,
        server_default="model_output"))
    op.add_column("workspace_file_versions", sa.Column("available", sa.Boolean(), nullable=True))
    op.add_column("chat_attachments", sa.Column("resource_file_id", UUID(as_uuid=True),
        sa.ForeignKey("workspace_files.id"), nullable=True))
    op.add_column("chat_attachments", sa.Column("resource_version", sa.Integer(), nullable=True))
    op.create_index("ix_chat_attachments_resource_file_id", "chat_attachments", ["resource_file_id"])
    connection = op.get_bind()
    attachments = connection.execute(sa.text("SELECT * FROM chat_attachments WHERE deleted_at IS NULL")).mappings().all()
    for row in attachments:
        path = f"uploads/{row['id']}/{row['filename']}"
        while connection.execute(sa.text("SELECT 1 FROM workspace_files WHERE workspace_id=:wid AND path=:path"),
                {"wid": row["workspace_id"], "path": path}).first():
            path = f"uploads/{row['id']}/{uuid4().hex}/{row['filename']}"
        connection.execute(sa.text("""
            INSERT INTO workspace_files (id, workspace_id, path, content_type, size_bytes, content_hash,
                current_version, created_by, updated_by, expires_at, created_at, updated_at, source)
            VALUES (:id, :workspace_id, :path, :content_type, :size_bytes, :content_hash, 1,
                :user_id, :user_id, '9999-12-31T00:00:00+00:00', :created_at, :created_at, 'chat_upload')
        """), {**dict(row), "path": path})
        connection.execute(sa.text("""
            INSERT INTO workspace_file_versions (id, file_id, workspace_id, version, object_key,
                content_type, size_bytes, content_hash, created_by, created_at)
            VALUES (:version_id, :id, :workspace_id, 1, :object_key, :content_type,
                :size_bytes, :content_hash, :user_id, :created_at)
        """), {**dict(row), "version_id": uuid4()})
        connection.execute(sa.text("UPDATE chat_attachments SET resource_file_id=:id, resource_version=1 WHERE id=:id"),
            {"id": row["id"]})
        connection.execute(sa.text("UPDATE chat_attachments SET resource_path=:path WHERE id=:id"),
            {"id": row["id"], "path": path})
    op.execute("UPDATE workspace_files SET expires_at = '9999-12-31T00:00:00+00:00' WHERE deleted_at IS NULL")
    op.execute("UPDATE workspace_storage_states SET over_quota_since = CURRENT_TIMESTAMP WHERE over_quota_since IS NOT NULL")


def downgrade():
    # Report the earlier, more specific accounting safety failure before
    # checking resource references on a long downgrade path.
    decimal = op.get_bind().execute(sa.text(
        'SELECT 1 FROM credit_accounts WHERE balance <> trunc(balance) '
        'OR balance NOT BETWEEN -2147483648 AND 2147483647 LIMIT 1')).first()
    if decimal:
        raise RuntimeError("Decimal credits exist; keep the decimal-compatible API and schema.")
    if op.get_bind().execute(sa.text("""
        SELECT 1 FROM chat_message_resources GROUP BY attachment_id HAVING COUNT(*) > 1 LIMIT 1
    """)).first():
        raise RuntimeError("Shared message references cannot be downgraded without data loss; restore a compatible application.")
    op.drop_column("workspace_file_versions", "available")
    op.drop_index("ix_chat_attachments_resource_file_id", table_name="chat_attachments")
    op.drop_column("chat_attachments", "resource_version")
    op.drop_column("chat_attachments", "resource_file_id")
    op.drop_column("workspace_files", "source")
    op.execute("""
        UPDATE chat_attachments AS a SET message_id = r.message_id
        FROM chat_message_resources AS r WHERE r.attachment_id = a.id
    """)
    op.drop_constraint("chat_attachments_message_id_fkey", "chat_attachments", type_="foreignkey")
    op.create_foreign_key("chat_attachments_message_id_fkey", "chat_attachments", "chat_messages",
        ["message_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("fk_chat_attachments_conversation_id", "chat_attachments", type_="foreignkey")
    op.drop_index("ix_chat_attachments_conversation_id", table_name="chat_attachments")
    op.drop_column("chat_attachments", "over_quota_deleted_at")
    op.drop_column("chat_attachments", "object_deleted_at")
    op.drop_column("chat_attachments", "resource_path")
    op.drop_column("chat_attachments", "conversation_id")
    op.drop_column("chat_attachments", "source")
    op.drop_table("chat_message_resources")
