"""Explicit, independently recoverable cloud computers; preserve existing records."""
from alembic import op
import sqlalchemy as sa

revision = "0076_explicit_cloud_computers"
down_revision = "0075_tool_cloud_merge"
branch_labels = depends_on = None


def upgrade():
    op.drop_index("uq_cloud_computer_active_user", table_name="cloud_computer_sessions")
    for column in (
        sa.Column("creation_request_id", sa.Uuid(), nullable=True),
        sa.Column("creation_digest", sa.String(64), nullable=True),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("cloud_computer_sessions", column)
    op.create_unique_constraint("uq_cloud_computer_creation", "cloud_computer_sessions", ["creation_request_id"])
    op.create_index("ix_cloud_computer_sessions_next_reconcile_at", "cloud_computer_sessions", ["next_reconcile_at"])
    connection = op.get_bind()
    settings = sa.table("cloud_computer_settings", sa.column("id", sa.Integer), sa.column("config", sa.JSON))
    for row in connection.execute(sa.select(settings.c.id, settings.c.config)):
        config = dict(row.config)
        config.pop("concurrency", None)
        connection.execute(settings.update().where(settings.c.id == row.id).values(config=config))
    connection.execute(sa.text(
        "UPDATE cloud_computer_sessions SET stop_requested_at = COALESCE(billing_stopped_at, created_at) "
        "WHERE status = 'stopping' AND ended_at IS NULL"))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT user_id FROM cloud_computer_sessions WHERE ended_at IS NULL "
        "GROUP BY user_id HAVING COUNT(*) > 1 LIMIT 1")).first():
        raise RuntimeError("存在多台活动云电脑，不能恢复单实例约束；请保留数据并先结束多余实例")
    op.create_index("uq_cloud_computer_active_user", "cloud_computer_sessions", ["user_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"), sqlite_where=sa.text("ended_at IS NULL"))
    op.drop_index("ix_cloud_computer_sessions_next_reconcile_at", table_name="cloud_computer_sessions")
    op.drop_constraint("uq_cloud_computer_creation", "cloud_computer_sessions", type_="unique")
    for name in ("creation_request_id", "creation_digest", "name", "stop_requested_at", "next_reconcile_at", "recovery_attempts"):
        op.drop_column("cloud_computer_sessions", name)
    settings = sa.table("cloud_computer_settings", sa.column("id", sa.Integer), sa.column("config", sa.JSON))
    for row in connection.execute(sa.select(settings.c.id, settings.c.config)):
        connection.execute(settings.update().where(settings.c.id == row.id)
            .values(config={**row.config, "concurrency": 5}))
