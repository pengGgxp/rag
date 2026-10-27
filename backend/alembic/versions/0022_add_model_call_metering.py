"""Add request-level model billing, durable calls and explicit default template.

Revision ID: 0022_add_model_call_metering
Revises: 0021_add_site_admin_console
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_add_model_call_metering"
down_revision = "0021_add_site_admin_console"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_billing_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("template", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"))
    table = sa.table("model_billing_settings", sa.column("id", sa.Integer()),
        sa.column("enabled", sa.Boolean()), sa.column("template", sa.JSON()), sa.column("version", sa.Integer()))
    op.bulk_insert(table, [dict(id=1, enabled=False, template={}, version=0)])
    op.create_table("model_usage_requests",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(24)),
        sa.Column("hold", sa.Integer(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True)))
    for column in ("workspace_id", "status"):
        op.create_index(f"ix_model_usage_requests_{column}", "model_usage_requests", [column])
    op.create_table("model_call_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), sa.ForeignKey("model_usage_requests.request_id"), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), sa.ForeignKey("llm_provider_configs.id")),
        sa.Column("provider_key", sa.String(80)),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("prices", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("request_id", "round", name="uq_model_call_round"))
    op.create_index("ix_model_call_usage_request_id", "model_call_usage", ["request_id"])


def downgrade():
    op.drop_table("model_call_usage")
    op.drop_table("model_usage_requests")
    op.drop_table("model_billing_settings")
