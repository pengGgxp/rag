"""Conversation title ownership and pin metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0062_conversation_management"
down_revision = "0061_resource_previews"
branch_labels = depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("title_source", sa.String(20), nullable=False, server_default="legacy"))
    op.add_column("conversations", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("conversations", sa.Column("pinned_at", sa.DateTime(timezone=True)))
    op.create_table("conversation_executions",
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False, index=True),
        sa.Column("message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id")),
        sa.Column("status", sa.String(24), nullable=False, index=True),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_table("conversation_events",
        sa.Column("request_id", UUID(as_uuid=True), sa.ForeignKey("conversation_executions.request_id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("encrypted_payload", sa.Text(), nullable=False))
    op.create_table("conversation_title_tasks",
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("request_id", UUID(as_uuid=True), sa.ForeignKey("conversation_executions.request_id"), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False), sa.Column("cost_reference", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table("conversation_title_tasks")
    op.drop_table("conversation_events")
    op.drop_table("conversation_executions")
    op.drop_column("conversations", "pinned_at")
    op.drop_column("conversations", "revision")
    op.drop_column("conversations", "title_source")
