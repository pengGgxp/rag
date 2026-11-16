"""Bind inline work to its durable Agent invocation without replaying old work."""
from alembic import op
import sqlalchemy as sa

revision = "0072_agent_tool_jobs"
down_revision = "0071_agent_tool_executions"
branch_labels = depends_on = None


def upgrade():
    for table in ("image_tasks", "cloud_computer_commands"):
        op.add_column(table, sa.Column("agent_tool_execution_id", sa.Uuid(), nullable=True))
        op.create_unique_constraint(f"uq_{table}_agent_tool", table, ["agent_tool_execution_id"])


def downgrade():
    for table in ("cloud_computer_commands", "image_tasks"):
        op.drop_constraint(f"uq_{table}_agent_tool", table, type_="unique")
        op.drop_column(table, "agent_tool_execution_id")
