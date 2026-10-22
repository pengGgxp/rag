"""Persist immutable recharge quotes and idempotent payment state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_add_recharge_orders"
down_revision = "0016_add_skill_files"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recharge_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("package_id", sa.String(64), nullable=False),
        sa.Column("package_name", sa.String(100), nullable=False),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("merchant_num", sa.String(100), nullable=False),
        sa.Column("pay_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("platform_order_no", sa.String(100), nullable=True),
        sa.Column("pay_url", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", "idempotency_key", name="uq_recharge_request"),
        sa.UniqueConstraint("merchant_num", "platform_order_no", name="uq_recharge_platform_order"),
        sa.CheckConstraint("amount_fen > 0 AND credits > 0", name="ck_recharge_positive"),
    )
    for column in ("workspace_id", "user_id", "created_at"):
        op.create_index(f"ix_recharge_orders_{column}", "recharge_orders", [column])


def downgrade():
    op.drop_table("recharge_orders")
