"""Add versioned site payment configuration."""
from alembic import op
import sqlalchemy as sa

revision = "0048_site_payment_settings"
down_revision = "0047_conversation_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("site_payment_configs",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("provider_key", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("api_base_url", sa.String(500), nullable=False),
        sa.Column("merchant_num", sa.String(100), nullable=False),
        sa.Column("pay_type", sa.String(100), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=True),
        sa.Column("packages", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("recharge_orders", sa.Column("provider_key", sa.String(40), nullable=False, server_default="zhifux"))
    op.add_column("recharge_orders", sa.Column("payment_config_version", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_recharge_payment_config_version", "recharge_orders", "site_payment_configs",
        ["payment_config_version"], ["version"])


def downgrade():
    op.drop_constraint("fk_recharge_payment_config_version", "recharge_orders", type_="foreignkey")
    op.drop_column("recharge_orders", "payment_config_version")
    op.drop_column("recharge_orders", "provider_key")
    op.drop_table("site_payment_configs")
