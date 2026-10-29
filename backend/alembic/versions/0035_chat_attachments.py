"""Private conversation attachments and explicit model image capability."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0035_chat_attachments"
down_revision = "0034_model_api_protocol"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_model_providers", sa.Column("supports_images", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("chat_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="CASCADE")),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text_content", sa.Text()),
        sa.Column("width", sa.Integer()), sa.Column("height", sa.Integer()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for field in ("workspace_id", "user_id", "message_id"):
        op.create_index(f"ix_chat_attachments_{field}", "chat_attachments", [field])


def downgrade():
    op.drop_table("chat_attachments")
    op.drop_column("site_model_providers", "supports_images")
