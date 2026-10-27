"""Shared price activation and immutable billing operation snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0025_workspace_billing"
down_revision = "0024_shared_model_providers"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("billing_multiplier_percent", sa.Integer(), server_default="100", nullable=False))
    op.add_column("workspaces", sa.Column("billing_version", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint("ck_workspace_billing_multiplier", "workspaces",
                               "billing_multiplier_percent BETWEEN 1 AND 10000")
    op.add_column("model_billing_settings", sa.Column("shared_pricing_enabled", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("model_billing_settings", sa.Column("pricing_paused", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("model_usage_requests", sa.Column("billing_context", sa.JSON(), server_default="{}", nullable=False))
    op.create_table("billing_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(100), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "request_id", "event_key", name="uq_billing_snapshot_operation"))
    op.create_index("ix_billing_snapshots_workspace_id", "billing_snapshots", ["workspace_id"])
    op.create_index("ix_billing_snapshots_request_id", "billing_snapshots", ["request_id"])


def downgrade():
    raise RuntimeError("Retain prices and consumption snapshots; use a forward-compatible rollback.")
