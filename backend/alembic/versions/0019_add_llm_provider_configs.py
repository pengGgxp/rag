"""add admin-managed chat model providers and provider-aware usage pricing

Revision ID: 0019_add_llm_provider_configs
Revises: 0018_add_usage_metering
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_add_llm_provider_configs"
down_revision = "0018_add_usage_metering"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), server_default="60", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_llm_provider_workspace_key"),
    )
    op.create_index("ix_llm_provider_configs_workspace_id", "llm_provider_configs", ["workspace_id"])

    op.add_column("usage_policies", sa.Column("provider_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_usage_policies_provider_id", "usage_policies", "llm_provider_configs",
        ["provider_id"], ["id"],
    )
    op.create_index("ix_usage_policies_provider_id", "usage_policies", ["provider_id"])
    op.drop_constraint("uq_usage_policy_workspace_capability", "usage_policies", type_="unique")
    op.create_unique_constraint(
        "uq_usage_policy_workspace_provider_capability",
        "usage_policies", ["workspace_id", "provider_id", "capability"],
    )

    op.add_column("usage_events", sa.Column("provider_id", sa.Uuid(), nullable=True))
    op.add_column("usage_events", sa.Column("provider_key", sa.String(length=80), nullable=True))
    op.create_foreign_key(
        "fk_usage_events_provider_id", "usage_events", "llm_provider_configs",
        ["provider_id"], ["id"],
    )
    op.create_index("ix_usage_events_provider_id", "usage_events", ["provider_id"])


def downgrade():
    op.drop_index("ix_usage_events_provider_id", table_name="usage_events")
    op.drop_constraint("fk_usage_events_provider_id", "usage_events", type_="foreignkey")
    op.drop_column("usage_events", "provider_key")
    op.drop_column("usage_events", "provider_id")
    op.drop_constraint("uq_usage_policy_workspace_provider_capability", "usage_policies", type_="unique")
    op.create_unique_constraint(
        "uq_usage_policy_workspace_capability",
        "usage_policies", ["workspace_id", "capability"],
    )
    op.drop_index("ix_usage_policies_provider_id", table_name="usage_policies")
    op.drop_constraint("fk_usage_policies_provider_id", "usage_policies", type_="foreignkey")
    op.drop_column("usage_policies", "provider_id")
    op.drop_index("ix_llm_provider_configs_workspace_id", table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
