"""Keep remote call identity independent of its arguments; preserve old receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0074_remote_tool_invocation"
down_revision = "0073_remove_tool_call_caps"
branch_labels = depends_on = None

LEGACY_COLUMNS = ["message_id", "tool_name", "arguments_digest"]


def upgrade():
    op.add_column("remote_tool_executions", sa.Column("invocation_id", sa.Uuid(), nullable=True))
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("remote_tool_executions"):
        if constraint["column_names"] == LEGACY_COLUMNS:
            op.drop_constraint(constraint["name"], "remote_tool_executions", type_="unique")
    op.create_unique_constraint("uq_remote_tool_invocation", "remote_tool_executions", ["invocation_id"])


def downgrade():
    duplicates = op.get_bind().execute(sa.text(
        "SELECT 1 FROM remote_tool_executions GROUP BY message_id, tool_name, arguments_digest "
        "HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicates:
        raise RuntimeError("Multiple invocations share arguments; preserve receipts and restore a compatible application.")
    op.create_unique_constraint("uq_remote_tool_legacy", "remote_tool_executions", LEGACY_COLUMNS)
    op.drop_constraint("uq_remote_tool_invocation", "remote_tool_executions", type_="unique")
    op.drop_column("remote_tool_executions", "invocation_id")
