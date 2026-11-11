"""Persist knowledge-base mentions on individual chat messages."""
from alembic import op
import sqlalchemy as sa

revision = "0064_conversation_knowledge_mentions"
down_revision = "0063_deployment_maintenance"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "chat_messages",
        sa.Column("knowledge_base_mentions", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade():
    op.drop_column("chat_messages", "knowledge_base_mentions")
