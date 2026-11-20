"""Merge Skill upload and ingestion retry migrations."""

revision = "0078_merge_retry_uploads"
down_revision = ("0077_direct_skill_uploads", "0077_ingestion_retry_progress")
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    pass
