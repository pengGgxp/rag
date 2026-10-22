"""Store multi-file Agent Skill resources without changing existing entrypoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_add_skill_files"
down_revision = "0015_add_agent_extensions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_skill_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("skill_id", "path"),
    )
    op.create_index("ix_agent_skill_files_skill_id", "agent_skill_files", ["skill_id"])


def downgrade():
    op.drop_table("agent_skill_files")
