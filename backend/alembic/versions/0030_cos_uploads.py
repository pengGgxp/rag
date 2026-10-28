"""Site COS settings and durable scoped multipart upload sessions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_cos_uploads"
down_revision = "0029_conversation_streaming"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("site_storage_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bucket", sa.String(100), nullable=False),
        sa.Column("region", sa.String(60), nullable=False),
        sa.Column("encrypted_secret_id", sa.Text()),
        sa.Column("encrypted_secret_key", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("tested_version", sa.Integer()),
        sa.Column("test_result", sa.String(200)))
    op.create_table("document_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("version_of", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id")),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("multipart_id", sa.String(500), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("parts", sa.JSON(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id")),
        sa.Column("error", sa.String(300)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for name in ("user_id", "workspace_id", "knowledge_base_id", "state", "expires_at"):
        op.create_index(f"ix_document_uploads_{name}", "document_uploads", [name])


def downgrade():
    op.drop_table("document_uploads")
    op.drop_table("site_storage_settings")
