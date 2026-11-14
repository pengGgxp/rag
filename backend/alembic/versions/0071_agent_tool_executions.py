"""Keep tool intent and full encrypted results independent of the live window."""
from alembic import op
import sqlalchemy as sa

revision = "0071_agent_tool_executions"
down_revision = "0070_workspace_file_operations"
branch_labels = depends_on = None


def upgrade():
    op.create_table("agent_tool_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("model_item_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("encrypted_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_agent_tool_executions_conversation_id", "agent_tool_executions", ["conversation_id"])
    op.create_index("ix_agent_tool_executions_request_id", "agent_tool_executions", ["request_id"])


def downgrade():
    op.drop_table("agent_tool_executions")
