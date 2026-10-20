"""add persistent knowledge conversations

Revision ID: 0006_add_conversations
Revises: 0005_add_query_traces
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_add_conversations"
down_revision = "0005_add_query_traces"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("active_request_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_table(
        "conversation_libraries",
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("knowledge_base_id", sa.Uuid(), sa.ForeignKey("knowledge_bases.id"), primary_key=True),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True, unique=True),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("traces", sa.JSON(), nullable=False),
        sa.Column("retrieval_query", sa.Text(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=False),
        sa.Column("refusal_reason", sa.String(100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_chat_message_sequence"),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])


def downgrade():
    op.drop_table("chat_messages")
    op.drop_table("conversation_libraries")
    op.drop_table("conversations")
