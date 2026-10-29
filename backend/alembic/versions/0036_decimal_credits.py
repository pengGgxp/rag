"""Four-decimal accounting without changing historic numeric amounts."""
from alembic import op
import sqlalchemy as sa

revision = "0036_decimal_credits"
down_revision = "0035_chat_attachments"
branch_labels = None
depends_on = None

COLUMNS = {
    "credit_accounts": ("balance", "lifetime_granted", "lifetime_spent"),
    "credit_ledger": ("amount", "balance_after"),
    "usage_events": ("credits",),
    "usage_policies": ("credits_per_unit",),
    "model_usage_requests": ("hold", "credits"),
}


def upgrade():
    for table, columns in COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, existing_type=sa.Integer(), type_=sa.Numeric(24, 4),
                            existing_nullable=False)


def downgrade():
    connection = op.get_bind()
    for table, columns in COLUMNS.items():
        for column in columns:
            unsafe = connection.execute(sa.text(
                f'SELECT 1 FROM "{table}" WHERE "{column}" <> trunc("{column}") '
                f'OR "{column}" NOT BETWEEN -2147483648 AND 2147483647 LIMIT 1')).first()
            if unsafe:
                raise RuntimeError("Decimal credits exist; keep the decimal-compatible API and schema.")
    for table, columns in COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, existing_type=sa.Numeric(24, 4), type_=sa.Integer(),
                            existing_nullable=False)
