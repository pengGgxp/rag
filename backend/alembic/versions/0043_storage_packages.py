"""Prepaid workspace storage and metered download allocations (no historic charges)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0043_storage_packages"
down_revision = "0042_workspace_file_retention"
branch_labels = depends_on = None


def upgrade():
    op.create_table("storage_quota_settings",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("free_storage_bytes", sa.BigInteger(), nullable=False),
        sa.Column("free_download_bytes", sa.BigInteger(), nullable=False),
        sa.Column("output_limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("packages", sa.JSON(), nullable=False), sa.Column("costs", sa.JSON(), nullable=False))
    op.create_table("workspace_storage_states",
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("over_quota_since", sa.DateTime(timezone=True)))
    op.create_table("storage_entitlements",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("name", sa.String(80), nullable=False),
        *[sa.Column(name, sa.BigInteger(), nullable=False) for name in
          ("storage_bytes", "download_bytes", "used_bytes", "reserved_bytes")],
        sa.Column("price_credits", sa.Numeric(24, 4), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "request_key", name="uq_storage_entitlement_request"))
    op.create_index("ix_storage_entitlements_workspace_id", "storage_entitlements", ["workspace_id"])
    op.create_index("ix_storage_entitlements_expires_at", "storage_entitlements", ["expires_at"])
    op.create_table("storage_transfers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("resource", sa.String(200), nullable=False),
        *[sa.Column(name, sa.BigInteger(), nullable=False) for name in
          ("requested_bytes", "sent_bytes", "uncertain_bytes", "inflight_bytes")],
        sa.Column("allocations", sa.JSON(), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    for name in ("workspace_id", "lease_until", "status"):
        op.create_index(f"ix_storage_transfers_{name}", "storage_transfers", [name])


def downgrade():
    op.drop_table("storage_transfers")
    op.drop_table("storage_entitlements")
    op.drop_table("workspace_storage_states")
    op.drop_table("storage_quota_settings")
