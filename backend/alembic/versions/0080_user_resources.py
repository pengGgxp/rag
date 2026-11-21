"""Make the receiving user the owner of files and explicit directories."""
from alembic import op
import sqlalchemy as sa


revision = "0080_user_resources"
down_revision = "0079_personal_account_ownership"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    op.drop_table("library_grants")
    op.drop_column("knowledge_bases", "restricted")
    op.alter_column("audit_logs", "owner_user_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("audit_logs", "workspace_id")
    constraints = {
        "workspace_files": "uq_workspace_file_path",
        "workspace_directories": "uq_workspace_directory_path",
    }
    for table, old_unique in constraints.items():
        op.add_column(table, sa.Column("owner_user_id", sa.Uuid(), nullable=True))
        connection.execute(sa.text(
            f"UPDATE {table} AS resource SET owner_user_id = ownership.user_id "
            "FROM personal_account_ownerships AS ownership "
            "WHERE resource.workspace_id = ownership.workspace_id"
        ))
        op.alter_column(table, "owner_user_id", existing_type=sa.Uuid(), nullable=False)
        op.create_foreign_key(f"fk_{table}_owner_user_id", table, "users", ["owner_user_id"], ["id"])
        op.drop_constraint(old_unique, table, type_="unique")
        op.create_unique_constraint(f"uq_{table}_owner_path", table, ["owner_user_id", "path"])


def downgrade():
    raise RuntimeError("Restore a pre-migration database backup to reverse the ownership transfer.")
