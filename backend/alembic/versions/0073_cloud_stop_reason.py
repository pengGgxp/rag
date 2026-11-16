"""Keep cloud termination cause separate from status and diagnostic errors."""
from alembic import op
import sqlalchemy as sa

revision = "0073_cloud_stop_reason"
down_revision = "0072_agent_tool_jobs"
branch_labels = depends_on = None


def upgrade():
    op.add_column("cloud_computer_sessions", sa.Column("stop_reason", sa.String(40), nullable=True))


def downgrade():
    op.drop_column("cloud_computer_sessions", "stop_reason")
