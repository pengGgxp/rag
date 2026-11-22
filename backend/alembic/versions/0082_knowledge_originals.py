"""Bind uploads and ingestion jobs to the expected original."""
from alembic import op
import sqlalchemy as sa

revision = "0082_knowledge_originals"
down_revision = "0081_direct_account_ownership"
branch_labels = depends_on = None


def upgrade():
    op.add_column("document_uploads", sa.Column("expected_content_hash", sa.String(64)))
    op.add_column("documents", sa.Column("original_revision", sa.Uuid()))
    op.add_column("ingestion_jobs", sa.Column("original_revision", sa.Uuid()))
    op.add_column("ingestion_jobs", sa.Column("original_hash", sa.String(64)))
    op.execute("UPDATE documents SET original_revision = id")
    op.execute("UPDATE ingestion_jobs SET original_revision = document_id")
    op.execute("UPDATE ingestion_jobs SET original_hash = (SELECT content_hash FROM documents "
               "WHERE documents.id = ingestion_jobs.document_id)")


def downgrade():
    op.drop_column("ingestion_jobs", "original_hash")
    op.drop_column("ingestion_jobs", "original_revision")
    op.drop_column("documents", "original_revision")
    op.drop_column("document_uploads", "expected_content_hash")
