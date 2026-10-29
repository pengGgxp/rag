"""Durable and idempotent administrator thinking tests."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0032_thinking_diagnostics"
down_revision = "0031_site_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column("encrypted_display_thinking", sa.Text()))
    op.add_column("chat_messages", sa.Column("thinking_diagnostics", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table("model_thinking_tests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("thinking_version", sa.Integer(), nullable=False),
        sa.Column("option_id", sa.String(40)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_model_thinking_tests_actor_user_id", "model_thinking_tests", ["actor_user_id"])


def downgrade():
    op.drop_table("model_thinking_tests")
    op.drop_column("chat_messages", "thinking_diagnostics")
    op.drop_column("chat_messages", "encrypted_display_thinking")
