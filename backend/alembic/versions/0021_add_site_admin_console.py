"""Add site admin suspension and unified site audit events.

Revision ID: 0021_add_site_admin_console
Revises: 0020_add_site_authentication
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_add_site_admin_console"
down_revision = "0020_add_site_authentication"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("is_suspended", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "site_audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(80), nullable=True),
        sa.Column("result", sa.String(20), nullable=False, server_default="success"),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_site_audit_events_created_at", "site_audit_events", ["created_at"])
    op.create_index("ix_site_audit_events_action", "site_audit_events", ["action"])
    op.create_index("ix_site_audit_events_target", "site_audit_events", ["target_type", "target_id"])
    # Backfill only known safe fields, retaining identifiers for traceability.
    connection = op.get_bind()
    source = sa.table("site_auth_audit", sa.column("id", sa.Uuid()), sa.column("actor_user_id", sa.Uuid()),
        sa.column("action", sa.String()), sa.column("details", sa.JSON()), sa.column("created_at", sa.DateTime(timezone=True)))
    target = sa.table("site_audit_events", *[sa.column(name, type_) for name, type_ in [
        ("id", sa.Uuid()), ("actor_user_id", sa.Uuid()), ("action", sa.String()),
        ("target_type", sa.String()), ("result", sa.String()), ("details", sa.JSON()),
        ("created_at", sa.DateTime(timezone=True))]])
    for row in connection.execute(sa.select(source)).mappings():
        details = {key: value for key, value in (row["details"] or {}).items() if key in {"fields", "accepted", "source"}}
        connection.execute(target.insert().values(id=row["id"], actor_user_id=row["actor_user_id"],
            action=row["action"], target_type="auth_settings", result="failed" if details.get("accepted") is False else "success",
            details=details, created_at=row["created_at"]))


def downgrade():
    op.drop_index("ix_site_audit_events_target", table_name="site_audit_events")
    op.drop_index("ix_site_audit_events_action", table_name="site_audit_events")
    op.drop_index("ix_site_audit_events_created_at", table_name="site_audit_events")
    op.drop_table("site_audit_events")
    op.drop_column("users", "is_suspended")
