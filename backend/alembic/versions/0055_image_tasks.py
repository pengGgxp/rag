"""Durable image jobs with frozen prices and encrypted credentials."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0055_image_tasks"
down_revision = "0054_model_capabilities"
branch_labels = depends_on = None


def upgrade():
    op.create_table("image_model_settings", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generate_model_id", UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id")),
        sa.Column("edit_model_id", UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table("image_tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id"), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("held_credits", sa.Numeric(24, 4), nullable=False),
        sa.Column("charged_credits", sa.Numeric(24, 4), nullable=False, server_default="0"),
        sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("upstream_request_id", sa.String(200)), sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=False), sa.Column("error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "actor_user_id", "idempotency_key", name="uq_image_task_request"))
    for column in ("workspace_id", "actor_user_id", "status"):
        op.create_index(f"ix_image_tasks_{column}", "image_tasks", [column])


def downgrade():
    op.drop_table("image_tasks")
    op.drop_table("image_model_settings")
