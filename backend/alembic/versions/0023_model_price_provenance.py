"""Record the template copied when a workspace is created."""
from alembic import op
import sqlalchemy as sa

revision = "0023_model_price_provenance"
down_revision = "0022_add_model_call_metering"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("usage_policies", sa.Column("template_version", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("usage_policies", "template_version")
