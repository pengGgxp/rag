"""Durable retry and compensation journal for ordinary workspace operations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0070_workspace_file_operations"
down_revision = "0069_workspace_directories"
branch_labels = depends_on = None


def upgrade():
    op.create_table("workspace_file_operations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "request_id", name="uq_workspace_file_operation_request"))
    op.create_index("ix_workspace_file_operations_workspace_id", "workspace_file_operations", ["workspace_id"])
    op.create_index("ix_workspace_file_operations_state", "workspace_file_operations", ["state"])


def downgrade():
    op.drop_table("workspace_file_operations")
