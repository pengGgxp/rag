"""Independent search credentials and durable paid tool calls; no default prices."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0037_search_tools"
down_revision = "0036_decimal_credits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("site_search_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("max_search_calls", sa.Integer(), nullable=False),
        sa.Column("max_fetch_calls", sa.Integer(), nullable=False),
        sa.Column("prices", sa.JSON(), nullable=False))
    op.create_table("search_tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("model_usage_requests.request_id"), nullable=False),
        sa.Column("call_key", sa.String(160), nullable=False),
        sa.Column("capability", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("price", sa.JSON(), nullable=False),
        sa.Column("encrypted_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("request_id", "call_key", name="uq_search_tool_call"))
    op.create_index("ix_search_tool_calls_request_id", "search_tool_calls", ["request_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM search_tool_calls LIMIT 1")).first():
        raise RuntimeError("Search accounting exists; retain the compatible schema and settlement.")
    op.drop_table("search_tool_calls")
    op.drop_table("site_search_settings")
