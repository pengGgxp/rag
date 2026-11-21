"""Replace workspace foreign keys with direct account ownership."""
from alembic import op
import sqlalchemy as sa


revision = "0081_direct_account_ownership"
down_revision = "0080_user_resources"
branch_labels = depends_on = None
UNCLAIMED = "00000000-0000-4000-a000-000000000001"

ACCOUNT_TABLES = (
    "agent_skills", "answer_feedback", "billing_snapshots", "chat_attachments",
    "cloud_computer_sessions", "conversation_document_saves", "conversations",
    "credit_ledger", "document_uploads", "image_tasks", "knowledge_base_tool_executions",
    "knowledge_base_file_snapshots", "knowledge_bases", "llm_provider_configs",
    "mcp_servers", "model_thinking_tests",
    "model_usage_requests", "product_suggestions", "question_workbench_grants",
    "recharge_orders", "remote_tool_executions", "resource_previews", "skill_uploads",
    "storage_entitlements", "storage_transfers", "usage_events", "usage_policies",
    "workspace_directories", "workspace_execution_jobs", "workspace_file_operations",
    "workspace_file_versions", "workspace_files", "workspace_skill_installs",
    "workspace_storage_states",
)


def upgrade():
    connection = op.get_bind()
    op.add_column("workspace_file_versions", sa.Column("mounted_object_key", sa.String(700), nullable=True))
    op.add_column("workspace_directories", sa.Column("mounted_marker_key", sa.String(700), nullable=True))
    op.add_column("users", sa.Column("legacy_resource_prefix", sa.String(100), nullable=True))
    op.create_table(
        "legacy_account_object_prefixes",
        sa.Column("prefix", sa.String(100), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_legacy_account_object_prefixes_user_id", "legacy_account_object_prefixes", ["user_id"])
    connection.execute(sa.text(
        "UPDATE users u SET legacy_resource_prefix = 'workspaces/' || o.workspace_id || '/resource-directory/' "
        "FROM personal_account_ownerships o WHERE u.id = o.user_id AND o.workspace_id = o.canonical_workspace_id"
    ))
    connection.execute(sa.text(
        "INSERT INTO legacy_account_object_prefixes (prefix, user_id) "
        "SELECT 'workspaces/' || workspace_id || '/', user_id FROM personal_account_ownerships"
    ))

    # Bindings can still be referenced by immutable usage records. Keep their
    # IDs and preserve the canonical binding as the active runtime reference.
    connection.execute(sa.text(
        "WITH ranked AS (SELECT r.id, ROW_NUMBER() OVER ("
        "PARTITION BY o.user_id, r.site_provider_id "
        "ORDER BY (r.workspace_id <> o.canonical_workspace_id), r.created_at, r.id) AS rank "
        "FROM llm_provider_configs r JOIN personal_account_ownerships o USING (workspace_id) "
        "WHERE r.site_provider_id IS NOT NULL) "
        "UPDATE llm_provider_configs r SET site_provider_id = NULL, active = false, enabled = false "
        "FROM ranked WHERE r.id = ranked.id AND ranked.rank > 1"
    ))
    connection.execute(sa.text(
        "WITH ranked AS (SELECT r.id, ROW_NUMBER() OVER (PARTITION BY o.user_id, r.key "
        "ORDER BY (r.workspace_id <> o.canonical_workspace_id), r.created_at, r.id) AS rank "
        "FROM llm_provider_configs r JOIN personal_account_ownerships o USING (workspace_id)) "
        "UPDATE llm_provider_configs r SET key = 'migration-' || r.id::text "
        "FROM ranked WHERE r.id = ranked.id AND ranked.rank > 1"
    ))
    connection.execute(sa.text(
        "DELETE FROM usage_policies r USING personal_account_ownerships o "
        "WHERE r.workspace_id = o.workspace_id AND r.provider_id IS NULL "
        "AND o.workspace_id <> o.canonical_workspace_id"
    ))
    connection.execute(sa.text(
        "DELETE FROM credit_ledger WHERE workspace_id = :seed "
        "AND idempotency_key = :key AND NOT EXISTS ("
        "SELECT 1 FROM personal_account_ownerships WHERE workspace_id = :seed)"
    ), {"seed": UNCLAIMED, "key": f"workspace:{UNCLAIMED}:initial"})
    # A source and another owner's target must never share an ID during the
    # in-place rewrite of unique keys.
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM personal_account_ownerships o "
        "JOIN personal_account_ownerships t ON t.workspace_id = o.user_id WHERE t.user_id <> o.user_id)"
    )):
        raise RuntimeError("Cannot rewrite ownership because a user ID collides with another account's legacy ID.")

    inspector = sa.inspect(connection)
    for table in ACCOUNT_TABLES:
        for constraint in inspector.get_foreign_keys(table):
            if constraint["referred_table"] == "workspaces":
                op.drop_constraint(constraint["name"], table, type_="foreignkey")
        op.alter_column(table, "workspace_id", new_column_name="account_id")
        connection.execute(sa.text(
            f"UPDATE {table} resource SET account_id = ownership.user_id "
            "FROM personal_account_ownerships ownership WHERE resource.account_id = ownership.workspace_id"
        ))
        op.create_foreign_key(f"fk_{table}_account_id", table, "users", ["account_id"], ["id"])

    op.drop_table("memberships")
    op.drop_table("personal_account_ownerships")
    op.drop_table("credit_accounts")
    op.drop_table("workspaces")


def downgrade():
    raise RuntimeError("Restore a pre-migration database backup to reverse the ownership transfer.")
