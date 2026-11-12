"""Persist authoritative replacement windows; migrate encrypted payloads on first authorized load."""
from alembic import op
import sqlalchemy as sa

revision = "0066_context_windows"
down_revision = "0065_resource_mentions"
branch_labels = depends_on = None


def upgrade():
    for name in ("window_id", "parent_window_id"):
        op.add_column("conversation_context_checkpoints", sa.Column(name, sa.Uuid(), nullable=True))
    op.add_column("conversation_context_checkpoints", sa.Column("parent_revision", sa.Integer(), nullable=True))
    op.add_column("conversation_context_checkpoints", sa.Column("encrypted_replacement", sa.Text(), nullable=True))
    op.add_column("conversation_context_checkpoints", sa.Column("budget", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("conversation_context_checkpoints", sa.Column("source", sa.String(24), nullable=False, server_default="legacy"))
    op.create_table("conversation_contexts",
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), sa.ForeignKey("conversation_context_checkpoints.id"), nullable=True),
        sa.Column("encrypted_suffix", sa.Text(), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("blocked_reason", sa.String(64), nullable=True),
        sa.Column("attempted_revision", sa.Integer(), nullable=True))


def downgrade():
    op.drop_table("conversation_contexts")
    for name in ("source", "budget", "encrypted_replacement", "parent_revision", "parent_window_id", "window_id"):
        op.drop_column("conversation_context_checkpoints", name)
