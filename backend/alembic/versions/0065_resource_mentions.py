"""Persist message-level resource locating metadata without pre-reading files."""
from alembic import op
import sqlalchemy as sa

revision = "0065_resource_mentions"
down_revision = "0064_knowledge_mentions"
branch_labels = depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column(
        "resource_mentions", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("chat_messages", "resource_mentions")
