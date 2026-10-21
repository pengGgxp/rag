"""add workspace credit accounts and ledger

Revision ID: 0011_add_credit_ledger
Revises: 0010_add_document_versions
"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0011_add_credit_ledger"
down_revision = "0010_add_document_versions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "credit_accounts",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lifetime_granted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lifetime_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency"),
    )
    op.create_index("ix_credit_ledger_workspace_id", "credit_ledger", ["workspace_id"])
    op.create_index("ix_credit_ledger_actor_user_id", "credit_ledger", ["actor_user_id"])
    op.create_index("ix_credit_ledger_created_at", "credit_ledger", ["created_at"])

    conn = op.get_bind()
    workspaces = conn.execute(sa.text("SELECT id FROM workspaces")).fetchall()
    account = sa.table(
        "credit_accounts",
        sa.column("workspace_id", sa.Uuid()),
        sa.column("balance", sa.Integer()),
        sa.column("lifetime_granted", sa.Integer()),
        sa.column("lifetime_spent", sa.Integer()),
    )
    ledger = sa.table(
        "credit_ledger",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("actor_user_id", sa.Uuid()),
        sa.column("amount", sa.Integer()),
        sa.column("balance_after", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("idempotency_key", sa.String()),
        sa.column("metadata", sa.JSON()),
    )
    for (workspace_id,) in workspaces:
        conn.execute(account.insert().values(
            workspace_id=workspace_id, balance=1000, lifetime_granted=1000, lifetime_spent=0,
        ))
        conn.execute(ledger.insert().values(
            id=uuid4(), workspace_id=workspace_id, actor_user_id=None, amount=1000,
            balance_after=1000, kind="initial_grant",
            idempotency_key=f"workspace:{workspace_id}:initial",
            metadata={"reason": "migration"},
        ))


def downgrade():
    op.drop_index("ix_credit_ledger_created_at", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_actor_user_id", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_workspace_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_table("credit_accounts")
