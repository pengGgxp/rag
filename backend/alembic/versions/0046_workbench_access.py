"""Make the question workbench an administrator-assigned user capability."""
from alembic import op
import sqlalchemy as sa

revision = "0046_workbench_access"
down_revision = "0045_question_workbench"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("question_workbench_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("question_workbench_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("users", "question_workbench_version")
    op.drop_column("users", "question_workbench_enabled")
