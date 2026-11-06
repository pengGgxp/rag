"""Cloud computer configuration, sessions and durable commands."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0056_cloud_computer"
down_revision = "0055_image_tasks"
branch_labels = depends_on = None


def upgrade():
    op.create_table("cloud_computer_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text()))
    op.create_table("cloud_computer_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(200)),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("held_credits", sa.Numeric(24, 4), nullable=False),
        sa.Column("charged_credits", sa.Numeric(24, 4), nullable=False),
        sa.Column("billed_seconds", sa.Integer(), nullable=False),
        sa.Column("billing_sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("billing_stopped_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    for column in ("workspace_id", "user_id", "conversation_id", "status"):
        op.create_index(f"ix_cloud_computer_sessions_{column}", "cloud_computer_sessions", [column])
    # Starting/uncertain sessions occupy their admission slot until reconciled.
    op.create_index("uq_cloud_computer_active_user", "cloud_computer_sessions", ["user_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"), sqlite_where=sa.text("ended_at IS NULL"))
    op.create_table("cloud_computer_commands",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("cloud_computer_sessions.id"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("process_id", sa.Integer()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "request_id", "digest", name="uq_cloud_command_request"))
    for column in ("session_id", "status"):
        op.create_index(f"ix_cloud_computer_commands_{column}", "cloud_computer_commands", [column])


def downgrade():
    op.drop_table("cloud_computer_commands")
    op.drop_table("cloud_computer_sessions")
    op.drop_table("cloud_computer_settings")
