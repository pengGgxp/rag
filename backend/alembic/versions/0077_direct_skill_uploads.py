"""Track direct-to-object-storage Skill package uploads."""
from alembic import op
import sqlalchemy as sa

revision = "0077_direct_skill_uploads"
down_revision = "0076_explicit_cloud_computers"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "skill_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("multipart_id", sa.String(500), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="uploading"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_skill_uploads_user_id", "skill_uploads", ["user_id"])
    op.create_index("ix_skill_uploads_workspace_id", "skill_uploads", ["workspace_id"])
    op.create_index("ix_skill_uploads_state", "skill_uploads", ["state"])
    op.create_index("ix_skill_uploads_expires_at", "skill_uploads", ["expires_at"])


def downgrade():
    op.drop_table("skill_uploads")
