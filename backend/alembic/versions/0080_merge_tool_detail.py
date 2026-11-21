"""Merge the retry-upload and tool-detail migration branches."""

revision = "0080_merge_tool_detail"
down_revision = ("0078_merge_retry_uploads", "0078_tool_detail")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
