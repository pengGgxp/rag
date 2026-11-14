"""Add explicit empty directories to the shared workspace."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0069_workspace_directories"
down_revision = "0068_workspace_skill_installs"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "workspace_directories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "path", name="uq_workspace_directory_path"),
    )
    op.create_index("ix_workspace_directories_workspace_id", "workspace_directories", ["workspace_id"])


def downgrade():
    op.drop_index("ix_workspace_directories_workspace_id", table_name="workspace_directories")
    op.drop_table("workspace_directories")
