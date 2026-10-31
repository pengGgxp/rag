from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0041_workspace_files"
down_revision = "0040_personal_extensions"
branch_labels = depends_on = None

def upgrade():
    op.create_table("workspace_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(512), nullable=False), sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "path", name="uq_workspace_file_path"))
    op.create_index("ix_workspace_files_workspace_id", "workspace_files", ["workspace_id"])
    op.create_table("workspace_file_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True), sa.Column("file_id", UUID(as_uuid=True), sa.ForeignKey("workspace_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(700), nullable=False, unique=True), sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("file_id", "version", name="uq_workspace_file_version"))
    op.create_index("ix_workspace_file_versions_file_id", "workspace_file_versions", ["file_id"])
    op.create_table("workspace_execution_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True), sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False), sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id")),
        sa.Column("request_id", UUID(as_uuid=True)), sa.Column("command_digest", sa.String(64), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("exit_code", sa.Integer()), sa.Column("stdout_summary", sa.Text(), nullable=False), sa.Column("stderr_summary", sa.Text(), nullable=False), sa.Column("changed_files", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_index("ix_workspace_execution_jobs_workspace_id", "workspace_execution_jobs", ["workspace_id"])

def downgrade():
    op.drop_table("workspace_execution_jobs")
    op.drop_table("workspace_file_versions")
    op.drop_table("workspace_files")
