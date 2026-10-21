"""add knowledge base tags

Revision ID: 0012_add_knowledge_base_tags
Revises: 0011_add_credit_ledger
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_add_knowledge_base_tags"
down_revision = "0011_add_credit_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_bases",
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.alter_column("knowledge_bases", "tags", server_default=None)


def downgrade():
    op.drop_column("knowledge_bases", "tags")
