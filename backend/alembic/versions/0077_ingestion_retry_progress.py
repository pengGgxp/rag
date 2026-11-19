"""Persist progress for automatic ingestion retries."""
from alembic import op
import sqlalchemy as sa

revision = "0077_ingestion_retry_progress"
down_revision = "0076_explicit_cloud_computers"
branch_labels = depends_on = None


def upgrade():
    op.add_column("ingestion_jobs", sa.Column("retry_progress", sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    op.drop_column("ingestion_jobs", "retry_progress")
