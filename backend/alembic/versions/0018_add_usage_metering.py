"""add configurable usage policies and idempotent usage events

Revision ID: 0018_add_usage_metering
Revises: 0017_add_recharge_orders
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_add_usage_metering"
down_revision = "0017_add_recharge_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "usage_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("unit_size", sa.Integer(), server_default="1", nullable=False),
        sa.Column("credits_per_unit", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "capability", name="uq_usage_policy_workspace_capability"),
        sa.CheckConstraint("credits_per_unit >= 0 AND unit_size > 0", name="ck_usage_policy_positive"),
    )
    op.create_index("ix_usage_policies_workspace_id", "usage_policies", ["workspace_id"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_usage_event_idempotency"),
    )
    for column in ("workspace_id", "actor_user_id", "request_id", "capability", "created_at"):
        op.create_index(f"ix_usage_events_{column}", "usage_events", [column])


def downgrade():
    for column in ("created_at", "capability", "request_id", "actor_user_id", "workspace_id"):
        op.drop_index(f"ix_usage_events_{column}", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("ix_usage_policies_workspace_id", table_name="usage_policies")
    op.drop_table("usage_policies")
