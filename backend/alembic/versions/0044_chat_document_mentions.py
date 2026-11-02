"""Persist per-turn document mentions for chat."""
from alembic import op
import sqlalchemy as sa

revision = "0044_chat_document_mentions"
down_revision = "0043_storage_packages"
branch_labels = depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column(
        "document_mentions", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("chat_messages", "document_mentions")
