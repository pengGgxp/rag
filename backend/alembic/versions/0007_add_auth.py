"""add accounts, sessions and workspace boundaries

Revision ID: 0007_add_auth
Revises: 0006_add_conversations
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_add_auth"
down_revision = "0006_add_conversations"
branch_labels = None
depends_on = None
LEGACY = "00000000-0000-4000-a000-000000000001"


def upgrade():
    op.create_table("users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("memberships",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.String(20), nullable=False))
    op.create_table("login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("csrf_token", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True))
    op.create_table("external_identities",
        sa.Column("provider", sa.String(500), primary_key=True),
        sa.Column("subject", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False, index=True))
    op.create_table("auth_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.execute(sa.text(
        "INSERT INTO workspaces (id, name) VALUES (CAST(:id AS uuid), :name)"
    ).bindparams(id=LEGACY, name="我的工作区"))
    for table in ("knowledge_bases", "conversations"):
        op.add_column(table, sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"),
                                      nullable=False, server_default=LEGACY))
        op.alter_column(table, "workspace_id", server_default=None)
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
    op.add_column("knowledge_bases", sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("knowledge_bases", "restricted", server_default=None)
    op.add_column("conversations", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.add_column("query_traces", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_query_traces_owner_id", "query_traces", ["owner_id"])
    op.create_table("library_grants",
        sa.Column("knowledge_base_id", sa.Uuid(), sa.ForeignKey("knowledge_bases.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True))


def downgrade():
    # Accounts own user data. Rollback must be an explicit backup/restore operation.
    raise RuntimeError("Restore a database backup to remove authentication without losing ownership boundaries.")
