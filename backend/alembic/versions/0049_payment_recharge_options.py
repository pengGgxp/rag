"""Persist administrator-controlled custom recharge option."""
from alembic import op
import sqlalchemy as sa

revision = "0049_payment_recharge_options"
down_revision = "0048_site_payment_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_payment_configs", sa.Column(
        "custom_recharge_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("site_payment_configs", "custom_recharge_enabled")
