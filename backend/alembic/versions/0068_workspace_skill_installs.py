"""Track platform Skill installs without copying platform content into a second store."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0068_workspace_skill_installs"
down_revision = "0067_context_tools_merge"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "workspace_skill_installs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_skill_id", UUID(as_uuid=True), sa.ForeignKey("site_skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("directory", sa.String(512), nullable=False, unique=True),
        sa.Column("source_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "site_skill_id", name="uq_workspace_skill_install"),
    )
    op.create_index("ix_workspace_skill_installs_workspace_id", "workspace_skill_installs", ["workspace_id"])
    op.create_index("ix_workspace_skill_installs_site_skill_id", "workspace_skill_installs", ["site_skill_id"])


def downgrade():
    op.drop_index("ix_workspace_skill_installs_site_skill_id", table_name="workspace_skill_installs")
    op.drop_index("ix_workspace_skill_installs_workspace_id", table_name="workspace_skill_installs")
    op.drop_table("workspace_skill_installs")
