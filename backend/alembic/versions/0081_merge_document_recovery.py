"""Merge document recovery into the existing test migration lineage."""

revision = "0081_merge_document_recovery"
down_revision = ("0079_document_recovery", "0080_merge_tool_detail")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
