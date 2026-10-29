"""Separate shared connections without changing model identities or prices."""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0033_model_connections"
down_revision = "0032_thinking_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column("site_model_providers", sa.Column("connection_id", postgresql.UUID(as_uuid=True)))
    db = op.get_bind()
    rows = db.execute(sa.text(
        "SELECT id, name, base_url, encrypted_api_key, timeout_seconds FROM site_model_providers")).mappings().all()
    for row in rows:
        identifier = uuid4()
        db.execute(sa.text("""INSERT INTO model_connections
            (id, name, platform, base_url, encrypted_api_key, timeout_seconds, enabled, version)
            VALUES (:id, :name, 'custom', :url, :secret, :timeout, true, 1)"""),
            {"id": identifier, "name": row["name"], "url": row["base_url"],
             "secret": row["encrypted_api_key"], "timeout": row["timeout_seconds"]})
        db.execute(sa.text("UPDATE site_model_providers SET connection_id=:connection WHERE id=:id"),
                   {"connection": identifier, "id": row["id"]})
    op.alter_column("site_model_providers", "connection_id", nullable=False)
    op.create_foreign_key("fk_model_connection", "site_model_providers", "model_connections", ["connection_id"], ["id"])
    op.create_index("ix_site_model_providers_connection_id", "site_model_providers", ["connection_id"])
    op.create_unique_constraint("uq_connection_model", "site_model_providers", ["connection_id", "model"])
    for column in ("base_url", "encrypted_api_key", "timeout_seconds"):
        op.drop_column("site_model_providers", column)
    op.add_column("model_thinking_tests", sa.Column("connection_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.add_column("site_model_providers", sa.Column("base_url", sa.Text(), nullable=False, server_default=""))
    op.add_column("site_model_providers", sa.Column("encrypted_api_key", sa.Text(), nullable=False, server_default=""))
    op.add_column("site_model_providers", sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="60"))
    op.execute("""UPDATE site_model_providers AS m SET base_url=c.base_url,
        encrypted_api_key=c.encrypted_api_key, timeout_seconds=c.timeout_seconds
        FROM model_connections AS c WHERE m.connection_id=c.id""")
    op.drop_column("model_thinking_tests", "connection_version")
    op.drop_constraint("uq_connection_model", "site_model_providers")
    op.drop_index("ix_site_model_providers_connection_id", table_name="site_model_providers")
    op.drop_constraint("fk_model_connection", "site_model_providers")
    op.drop_column("site_model_providers", "connection_id")
    op.drop_table("model_connections")
