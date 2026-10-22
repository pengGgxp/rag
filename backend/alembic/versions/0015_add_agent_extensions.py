"""Add user-owned MCP connections, skills, selections and tool event summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_add_agent_extensions"
down_revision = "0014_add_parser_locations"
branch_labels = None
depends_on = None


def upgrade():
    for table, columns in (
        ("agent_skills", [
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("markdown", sa.Text(), nullable=False),
        ]),
        ("mcp_servers", [
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("transport", sa.String(30), nullable=False),
            sa.Column("encrypted_headers", sa.Text(), nullable=True),
            sa.Column("header_names", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("tools", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("allowed_tools", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        ]),
    ):
        op.create_table(
            table,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
            *columns,
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    op.add_column("conversations", sa.Column("skill_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("conversations", sa.Column("mcp_server_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("chat_messages", sa.Column("tool_events", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("chat_messages", "tool_events")
    op.drop_column("conversations", "mcp_server_ids")
    op.drop_column("conversations", "skill_ids")
    op.drop_table("mcp_servers")
    op.drop_table("agent_skills")
