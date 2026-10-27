"""Add shared connections while retaining workspace provider and history IDs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0024_shared_model_providers"
down_revision = "0023_model_price_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("site_model_providers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prices", sa.JSON(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column("workspaces", sa.Column("model_version", sa.Integer(), server_default="0", nullable=False))
    op.add_column("llm_provider_configs", sa.Column("site_provider_id", UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id"), nullable=True))
    op.add_column("llm_provider_configs", sa.Column("source_price_version", sa.Integer(), nullable=True))
    op.create_index("ix_llm_provider_configs_site_provider_id", "llm_provider_configs", ["site_provider_id"])
    op.create_unique_constraint("uq_workspace_site_provider", "llm_provider_configs", ["workspace_id", "site_provider_id"])
    op.add_column("model_billing_settings", sa.Column("default_provider_id", UUID(as_uuid=True), sa.ForeignKey("site_model_providers.id"), nullable=True))
    op.add_column("model_billing_settings", sa.Column("connections_migrated", sa.Boolean(), server_default="false", nullable=False))


def downgrade():
    raise RuntimeError("Shared provider records must be retained; use a forward-compatible rollback.")
