"""add answer feedback

Revision ID: 0008_add_answer_feedback
Revises: 0007_add_auth
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_add_answer_feedback"
down_revision = "0007_add_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("rating", sa.String(10), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),
    )
    op.create_index("ix_answer_feedback_message_id", "answer_feedback", ["message_id"])
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"])
    op.create_index("ix_answer_feedback_workspace_id", "answer_feedback", ["workspace_id"])


def downgrade():
    op.drop_table("answer_feedback")
