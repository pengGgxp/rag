"""Rebuildable resource preview tasks, separate from resource storage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0061_resource_previews"
down_revision = "0060_cloud_http_preview"
branch_labels = depends_on = None


def upgrade():
    op.create_table("resource_previews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("resource_id", UUID(as_uuid=True), sa.ForeignKey("workspace_files.id"), nullable=False, index=True),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("object_key", sa.String(1000)),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer()), sa.Column("height", sa.Integer()),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("resource_id", "revision", "kind", name="uq_resource_preview_content"))


def downgrade():
    op.drop_table("resource_previews")
