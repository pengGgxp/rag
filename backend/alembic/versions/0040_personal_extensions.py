"""Restore personal extensions only after explicit review and authorization."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0040_personal_extensions"
down_revision = "0039_site_skill_files"
branch_labels = depends_on = None


def upgrade():
    op.add_column("agent_skills", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_skills", sa.Column("limitations_acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("mcp_servers", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("mcp_servers", sa.Column("authorization_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("conversations", sa.Column("extensions_initialized", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE agent_skills SET enabled=false")
    op.execute("UPDATE mcp_servers SET enabled=false, allowed_tools='[]'")
    op.alter_column("conversations", "extensions_initialized", server_default=sa.true())
    op.create_table("remote_tool_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("authorization_version", sa.Integer(), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("encrypted_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("message_id", "tool_name", "arguments_digest"))
    op.create_index("ix_remote_tool_executions_message_id", "remote_tool_executions", ["message_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM remote_tool_executions LIMIT 1")).first():
        raise RuntimeError("Preserve remote operation records; restore a compatible application instead.")
    op.drop_table("remote_tool_executions")
    op.drop_column("conversations", "extensions_initialized")
    op.drop_column("mcp_servers", "authorization_version")
    op.drop_column("mcp_servers", "version")
    op.drop_column("agent_skills", "limitations_acknowledged")
    op.drop_column("agent_skills", "version")
