"""Independent storage providers and resumable object migration."""
from alembic import op
import sqlalchemy as sa

revision = "0058_oss_storage"
down_revision = "0057_cloud_public_domains"
branch_labels = depends_on = None


def upgrade():
    op.add_column("site_storage_settings", sa.Column("provider", sa.String(12), nullable=False, server_default="cos"))
    op.create_index("uq_storage_provider", "site_storage_settings", ["provider"], unique=True)
    op.create_index("uq_storage_active", "site_storage_settings", ["active"], unique=True,
        postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"))
    op.create_table("storage_migrations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_provider", sa.String(12), nullable=False),
        sa.Column("target_provider", sa.String(12), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_table("storage_migration_objects",
        sa.Column("migration_id", sa.Uuid(), sa.ForeignKey("storage_migrations.id"), primary_key=True),
        sa.Column("object_key", sa.String(700), primary_key=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table("storage_migration_objects")
    op.drop_table("storage_migrations")
    op.drop_index("uq_storage_active", table_name="site_storage_settings")
    op.drop_index("uq_storage_provider", table_name="site_storage_settings")
    op.drop_column("site_storage_settings", "provider")
