"""Add versioned model-specific thinking capabilities."""
from alembic import op
import sqlalchemy as sa

revision = "0027_model_thinking"
down_revision = "0026_message_model"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_model_providers", sa.Column("thinking_config", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("site_model_providers", sa.Column("thinking_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("site_model_providers", "thinking_version")
    op.drop_column("site_model_providers", "thinking_config")
