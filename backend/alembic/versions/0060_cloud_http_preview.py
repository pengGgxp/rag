"""Durable HTTP preview processes and short-lived host-bound grants."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0060_cloud_http_preview"
down_revision = "0059_shared_resource_mount"
branch_labels = depends_on = None


def upgrade():
    op.create_table("cloud_previews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("cloud_computer_sessions.id"), nullable=False),
        sa.Column("command_id", UUID(as_uuid=True), sa.ForeignKey("cloud_computer_commands.id"), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("upstream_host", sa.String(300)),
        sa.Column("encrypted_traffic_token", sa.Text()), sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500)), sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_cloud_previews_session_id", "cloud_previews", ["session_id"])
    op.create_index("ix_cloud_previews_status", "cloud_previews", ["status"])
    op.create_index("uq_cloud_preview_active_session", "cloud_previews", ["session_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"), sqlite_where=sa.text("ended_at IS NULL"))
    op.create_table("cloud_preview_grants",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("preview_id", UUID(as_uuid=True), sa.ForeignKey("cloud_previews.id"), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_cloud_preview_grants_preview_id", "cloud_preview_grants", ["preview_id"])
    op.create_index("ix_cloud_preview_grants_expires_at", "cloud_preview_grants", ["expires_at"])


def downgrade():
    op.drop_table("cloud_preview_grants")
    op.drop_table("cloud_previews")
