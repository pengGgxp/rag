"""Configurable registration grant and a published site announcement."""
from alembic import op
import sqlalchemy as sa

revision = "0031_site_settings"
down_revision = "0030_cos_uploads"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table("site_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("signup_credits", sa.Integer(), nullable=False),
        sa.Column("announcement_title", sa.String(120), nullable=False),
        sa.Column("announcement_body", sa.Text(), nullable=False),
        sa.Column("announcement_published", sa.Boolean(), nullable=False),
        sa.Column("announcement_revision", sa.Integer(), nullable=False),
        sa.Column("announcement_updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("signup_credits BETWEEN 0 AND 1000000", name="ck_site_signup_credits"))
    op.bulk_insert(table, [{
        "id": 1, "version": 0, "signup_credits": 1000,
        "announcement_title": "", "announcement_body": "",
        "announcement_published": False, "announcement_revision": 0,
    }])


def downgrade():
    op.drop_table("site_settings")
