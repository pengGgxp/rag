"""Remove task-level call caps without changing prices or usage history."""
from alembic import op
import sqlalchemy as sa

revision = "0073_remove_tool_call_caps"
down_revision = "0072_agent_tool_jobs"
branch_labels = depends_on = None


def upgrade():
    op.drop_column("site_search_settings", "max_search_calls")
    op.drop_column("site_search_settings", "max_fetch_calls")


def downgrade():
    op.add_column("site_search_settings", sa.Column("max_search_calls", sa.Integer(),
                                                   nullable=False, server_default="3"))
    op.add_column("site_search_settings", sa.Column("max_fetch_calls", sa.Integer(),
                                                   nullable=False, server_default="5"))
