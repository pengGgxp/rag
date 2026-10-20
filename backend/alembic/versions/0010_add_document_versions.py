"""add document version metadata

Revision ID: 0010_add_document_versions
Revises: 0009_add_audit_logs
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_add_document_versions"
down_revision = "0009_add_audit_logs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("version_group_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("version_number", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("is_active", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("supersedes_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_documents_supersedes_id", "documents", "documents", ["supersedes_id"], ["id"]
    )
    op.execute("UPDATE documents SET version_group_id = id, version_number = 1, is_active = TRUE")
    op.alter_column("documents", "version_group_id", nullable=False)
    op.alter_column("documents", "version_number", nullable=False)
    op.alter_column("documents", "is_active", nullable=False)
    op.create_index("ix_documents_version_group_id", "documents", ["version_group_id"])
    op.create_index("ix_documents_is_active", "documents", ["is_active"])


def downgrade():
    op.drop_index("ix_documents_is_active", table_name="documents")
    op.drop_index("ix_documents_version_group_id", table_name="documents")
    op.drop_constraint("fk_documents_supersedes_id", "documents", type_="foreignkey")
    op.drop_column("documents", "supersedes_id")
    op.drop_column("documents", "is_active")
    op.drop_column("documents", "version_number")
    op.drop_column("documents", "version_group_id")
