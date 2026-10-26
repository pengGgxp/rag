"""Add verified email, site administrator and temporary authentication flows.

Revision ID: 0020_add_site_authentication
Revises: 0019_add_llm_provider_configs
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_add_site_authentication"
down_revision = "0019_add_llm_provider_configs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("is_site_admin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "authentication_flows",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_authentication_flows_expires_at", "authentication_flows", ["expires_at"])
    op.create_table("site_auth_settings", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("config", sa.JSON(), nullable=False),
                    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("site_auth_audit", sa.Column("id", sa.Uuid(), primary_key=True),
                    sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("action", sa.String(80), nullable=False),
                    sa.Column("details", sa.JSON(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    op.drop_table("site_auth_audit")
    op.drop_table("site_auth_settings")
    op.drop_index("ix_authentication_flows_expires_at", table_name="authentication_flows")
    op.drop_table("authentication_flows")
    op.drop_column("users", "is_site_admin")
    op.drop_column("users", "email_verified_at")
