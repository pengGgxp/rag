"""Fingerprint the persistent workspace resource directory."""
from alembic import op
import sqlalchemy as sa

revision = "0059_shared_resource_mount"
down_revision = "0058_oss_storage"
branch_labels = depends_on = None


def upgrade():
    op.add_column("workspace_files", sa.Column("live_etag", sa.String(200), nullable=True))


def downgrade():
    op.drop_column("workspace_files", "live_etag")
