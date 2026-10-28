"""Preserve ordered message blocks, context checkpoints and explicit model budgets."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_conversation_streaming"
down_revision = "0028_message_thinking"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column("display_blocks", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("site_model_providers", sa.Column("context_capacity", sa.Integer(), nullable=True))
    op.add_column("site_model_providers", sa.Column("max_output_tokens", sa.Integer(), nullable=True))
    op.create_table("conversation_context_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("through_sequence", sa.Integer(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("llm_provider_configs.id"), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("estimated_before", sa.Integer(), nullable=False),
        sa.Column("estimated_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_conversation_context_checkpoints_conversation_id", "conversation_context_checkpoints", ["conversation_id"])
    op.create_index("ix_conversation_context_checkpoints_request_id", "conversation_context_checkpoints", ["request_id"])
    op.create_table("conversation_context_operations",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_conversation_context_operations_conversation_id", "conversation_context_operations", ["conversation_id"])


def downgrade():
    op.drop_table("conversation_context_operations")
    op.drop_table("conversation_context_checkpoints")
    op.drop_column("chat_messages", "display_blocks")
    op.drop_column("site_model_providers", "max_output_tokens")
    op.drop_column("site_model_providers", "context_capacity")
