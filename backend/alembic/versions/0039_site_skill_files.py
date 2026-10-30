"""Preserve reviewed platform Skill package resources."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0039_site_skill_files"
down_revision = "0038_platform_skills_suggestions"
branch_labels = depends_on = None


def upgrade():
    op.create_table("site_skill_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("skill_id", UUID(as_uuid=True), sa.ForeignKey("site_skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("skill_id", "path"))
    op.create_index("ix_site_skill_files_skill_id", "site_skill_files", ["skill_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM site_skill_files LIMIT 1")).first():
        raise RuntimeError("Retain uploaded Skill resources; restore a compatible application instead.")
    op.drop_table("site_skill_files")
