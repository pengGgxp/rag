"""Track generated documents and recoverable conversation ingestion submissions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0047_conversation_documents"
down_revision = "0046_workbench_access"
branch_labels = depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("content_origin", sa.String(30), nullable=False, server_default="uploaded"))
    op.create_table("conversation_document_saves",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_base_id", UUID(as_uuid=True), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("ingestion_jobs.id"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_citations", sa.JSON(), nullable=False),
        sa.Column("dispatch_pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "request_id", "knowledge_base_id", "content_hash", name="uq_conversation_document_save"))
    for column in ("workspace_id", "user_id", "conversation_id", "document_id", "dispatch_pending"):
        op.create_index(f"ix_conversation_document_saves_{column}", "conversation_document_saves", [column])


def downgrade():
    op.drop_table("conversation_document_saves")
    op.drop_column("documents", "content_origin")
