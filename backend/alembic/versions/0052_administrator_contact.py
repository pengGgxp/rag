"""Configurable administrator contact for user feedback."""
from alembic import op
import sqlalchemy as sa

revision = "0052_administrator_contact"
down_revision = "0051_recharge_checkout"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_settings", sa.Column("administrator_contact", sa.Text(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("site_settings", "administrator_contact")
