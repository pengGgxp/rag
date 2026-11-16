"""Join independently published tool-runtime and cloud-ending revisions."""

revision = "0075_tool_cloud_merge"
down_revision = ("0073_cloud_stop_reason", "0074_remote_tool_invocation")
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    pass
