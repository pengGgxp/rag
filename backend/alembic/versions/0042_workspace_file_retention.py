from alembic import op
import sqlalchemy as sa

revision = "0042_workspace_file_retention"
down_revision = "0041_workspace_files"
branch_labels = depends_on = None


def upgrade():
    op.add_column("workspace_files", sa.Column(
        "expires_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE workspace_files SET expires_at = NOW() + INTERVAL '3 days' WHERE expires_at IS NULL")
    op.alter_column("workspace_files", "expires_at", nullable=False, server_default=sa.func.now())
    op.create_index("ix_workspace_files_expires_at", "workspace_files", ["expires_at"])


def downgrade():
    op.drop_index("ix_workspace_files_expires_at", table_name="workspace_files")
    op.drop_column("workspace_files", "expires_at")
