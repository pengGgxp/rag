"""Link knowledge documents to workspace files and retain command snapshots."""
from alembic import op
import sqlalchemy as sa


revision = "0077_knowledge_workspace_files"
down_revision = "0076_explicit_cloud_computers"
branch_labels = depends_on = None


def upgrade():
    op.add_column("document_uploads", sa.Column("workspace_path", sa.String(512), nullable=True))
    op.add_column("document_uploads", sa.Column("workspace_revision", sa.String(64), nullable=True))
    op.add_column("knowledge_bases", sa.Column("workspace_path", sa.String(512), nullable=True))
    op.create_index("ix_knowledge_bases_workspace_path", "knowledge_bases",
        ["workspace_id", "workspace_path"], unique=True)
    op.add_column("documents", sa.Column("workspace_file_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("workspace_path", sa.String(512), nullable=True))
    op.create_foreign_key("fk_documents_workspace_file_id_workspace_files",
        "documents", "workspace_files", ["workspace_file_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_documents_workspace_file_id", "documents", ["workspace_file_id"])
    op.create_index("ix_documents_workspace_path", "documents", ["workspace_path"])
    op.create_table(
        "knowledge_base_file_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("snapshots", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "request_id", name="uq_knowledge_base_file_snapshot_request"),
    )
    op.create_index("ix_knowledge_base_file_snapshots_workspace_id",
        "knowledge_base_file_snapshots", ["workspace_id"])
    op.create_index("ix_knowledge_base_file_snapshots_state",
        "knowledge_base_file_snapshots", ["state"])


def downgrade():
    op.drop_index("ix_knowledge_base_file_snapshots_state", table_name="knowledge_base_file_snapshots")
    op.drop_index("ix_knowledge_base_file_snapshots_workspace_id", table_name="knowledge_base_file_snapshots")
    op.drop_table("knowledge_base_file_snapshots")
    op.drop_index("ix_documents_workspace_path", table_name="documents")
    op.drop_index("ix_documents_workspace_file_id", table_name="documents")
    op.drop_constraint("fk_documents_workspace_file_id_workspace_files", "documents", type_="foreignkey")
    op.drop_column("documents", "workspace_path")
    op.drop_column("documents", "workspace_file_id")
    op.drop_index("ix_knowledge_bases_workspace_path", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "workspace_path")
    op.drop_column("document_uploads", "workspace_revision")
    op.drop_column("document_uploads", "workspace_path")
