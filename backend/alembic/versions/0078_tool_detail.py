"""Retain encrypted tool arguments for authorized conversation details."""
from alembic import op
import sqlalchemy as sa

revision = "0078_tool_detail"
down_revision = "0077_direct_skill_uploads"
branch_labels = depends_on = None


def upgrade():
    op.add_column("agent_tool_executions", sa.Column("encrypted_arguments", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("agent_tool_executions", "encrypted_arguments")
