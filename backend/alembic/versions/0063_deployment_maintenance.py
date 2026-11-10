"""Persistent deployment maintenance state."""
from alembic import op
import sqlalchemy as sa

revision = "0063_deployment_maintenance"
down_revision = "0062_conversation_management"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "deployment_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("release", sa.String(120)),
        sa.Column("message", sa.String(300)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "INSERT INTO deployment_state (id, active, version) "
        "VALUES (1, false, 1) ON CONFLICT (id) DO NOTHING"
    )


def downgrade():
    op.drop_table("deployment_state")
