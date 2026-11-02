"""First-party question workbench grants and conversation selection."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0045_question_workbench"
down_revision = "0044_chat_document_mentions"
branch_labels = depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("question_workbench", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("question_workbench_grants",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("question_workbench_grants")
    op.drop_column("conversations", "question_workbench")
