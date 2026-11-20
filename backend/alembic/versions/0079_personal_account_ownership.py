"""Prepare deterministic user ownership and user-level billing policy.

This migration intentionally keeps the legacy workspace columns and tables in
place. Runtime authorization is moved separately so deployments never enter a
state where live code expects user-keyed billing while the database still has
only workspace-keyed balances.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0079_personal_account_ownership"
down_revision = "0078_merge_retry_uploads"
branch_labels = None
depends_on = None
LEGACY = "00000000-0000-4000-a000-000000000001"


def _merge_resource_paths(connection):
    # Plan both namespaces together. A file cannot hide a directory (including
    # an implicit parent), and even a tombstone still occupies a unique path.
    by_owner = {}
    for name in ("workspace_files", "workspace_directories"):
        rows = connection.execute(sa.text(
            f"SELECT r.id, r.path, o.canonical_workspace_id AS target, w.created_at, r.workspace_id "
            f"FROM {name} r JOIN personal_account_ownerships o USING (workspace_id) "
            "JOIN workspaces w ON w.id = r.workspace_id"
        )).mappings()
        for row in rows:
            by_owner.setdefault(row["target"], []).append((name, row))

    for target, rows in by_owner.items():
        if all(row["workspace_id"] == target for _, row in rows):
            continue
        files, directories = set(), set()
        seen_directories = set()
        # Keep the canonical workspace's names, then assign stable names in
        # workspace order. Reserve original names so a suffix cannot steal one.
        originals = {row["path"] for _, row in rows}
        directory_aliases = {}
        for name, row in sorted(rows, key=lambda entry: (
            entry[1]["workspace_id"] != target, entry[1]["created_at"], entry[1]["workspace_id"],
            entry[1]["path"].count("/"), entry[1]["path"], entry[0], entry[1]["id"]
        )):
            old_path = row["path"]
            original_parts = old_path.split("/")
            path_parts = []
            for index, part in enumerate(original_parts):
                final = index == len(original_parts) - 1
                is_file = final and name == "workspace_files"
                alias_key = (row["workspace_id"], "/".join(original_parts[:index + 1]))
                if is_file:
                    chosen = part
                else:
                    chosen = directory_aliases.get(alias_key, part)
                candidate = "/".join([*path_parts, chosen])
                occupied = files | (directories if is_file else seen_directories if final else set())
                if candidate in occupied:
                    stem, dot, ext = part.rpartition(".") if is_file else ("", "", "")
                    if not stem:
                        stem, dot, ext = part, "", ""
                    for number in range(2, len(rows) + len(originals) + 3):
                        suffix = f" ({number})"
                        room = 512 - len("/".join(path_parts)) - bool(path_parts)
                        # Leave room for all original descendants of an alias.
                        if not final or not is_file:
                            prefix = alias_key[1] + "/"
                            room -= max((len(p) - len(prefix) + 1 for p in originals if p.startswith(prefix)), default=0)
                        extension = f"{dot}{ext}"[:max(0, room - len(suffix) - 1)]
                        if room <= len(suffix):
                            raise RuntimeError(f"Cannot rename conflicting resource path {old_path!r} within 512 characters")
                        chosen = stem[:room - len(suffix) - len(extension)] + suffix + extension
                        candidate = "/".join([*path_parts, chosen])
                        if candidate not in occupied and candidate not in originals:
                            break
                    else:
                        raise RuntimeError(f"Cannot allocate a distinct resource path for {old_path!r}")
                if not is_file:
                    directory_aliases[alias_key] = chosen
                    directories.add(candidate)
                    if final:
                        seen_directories.add(candidate)
                else:
                    files.add(candidate)
                path_parts.append(chosen)
            new_path = "/".join(path_parts)
            if row["workspace_id"] != target or old_path != new_path:
                connection.execute(sa.text(
                    f"UPDATE {name} SET workspace_id = :target, path = :path WHERE id = :id"
                ), {"target": target, "path": new_path, "id": row["id"]})
                if name == "workspace_files" and old_path != new_path:
                    connection.execute(sa.text(
                        "UPDATE chat_attachments SET resource_path = :path WHERE resource_file_id = :id"
                    ), {"path": new_path, "id": row["id"]})


def _unclaimed_seed(connection, workspace_id):
    if str(workspace_id) != LEGACY:
        return False
    inspector = sa.inspect(connection)
    for name in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns(name)}
        if "workspace_id" not in columns:
            continue
        table = sa.Table(name, sa.MetaData(), autoload_with=connection, resolve_fks=False)
        query = sa.select(1).select_from(table).where(table.c.workspace_id == workspace_id)
        if name == "credit_accounts":
            query = query.where(sa.not_(sa.and_(table.c.balance == 1000,
                table.c.lifetime_granted == 1000, table.c.lifetime_spent == 0)))
        elif name == "credit_ledger":
            query = query.where(sa.not_(sa.and_(table.c.idempotency_key == f"workspace:{LEGACY}:initial",
                table.c.kind == "initial_grant", table.c.amount == 1000, table.c.balance_after == 1000,
                table.c.actor_user_id.is_(None))))
        if connection.scalar(query.limit(1)) is not None:
            return False
    return True


def _resolve_merge_collisions(connection):
    # These are replay keys scoped to the source account, not financial IDs.
    # Keep the first key addressable and keep every historical row and amount.
    for table, key, dimension in (
        ("billing_snapshots", "event_key", "r.request_id, "),
        ("image_tasks", "idempotency_key", ""),
        ("recharge_orders", "idempotency_key", ""),
        ("workspace_file_operations", "request_id", ""),
    ):
        connection.execute(sa.text(
            f"WITH ranked AS (SELECT r.id, ROW_NUMBER() OVER ("
            f"PARTITION BY o.canonical_workspace_id, {dimension}r.{key} "
            "ORDER BY (r.workspace_id <> o.canonical_workspace_id), r.created_at, r.id) AS rank "
            f"FROM {table} r JOIN personal_account_ownerships o USING (workspace_id)) "
            f"UPDATE {table} r SET {key} = 'migration:' || r.id::text "
            "FROM ranked WHERE r.id = ranked.id AND ranked.rank > 1"
        ))

    # Grants express individual consent, so do not transfer other members'
    # grants. Keep the canonical grant (including an explicit revocation).
    connection.execute(sa.text(
        "WITH ranked AS (SELECT g.user_id, g.workspace_id, "
        "g.user_id = o.user_id AS is_receiver, "
        "ROW_NUMBER() OVER (PARTITION BY g.user_id, o.canonical_workspace_id "
        "ORDER BY (g.workspace_id <> o.canonical_workspace_id), w.created_at, w.id) AS rank "
        "FROM question_workbench_grants g JOIN personal_account_ownerships o USING (workspace_id) "
        "JOIN workspaces w ON w.id = g.workspace_id) "
        "DELETE FROM question_workbench_grants g USING ranked "
        "WHERE g.user_id = ranked.user_id AND g.workspace_id = ranked.workspace_id "
        "AND (NOT ranked.is_receiver OR ranked.rank > 1)"
    ))

    # One install record per site Skill. Its former files remain in the
    # resource namespace and are handled by the same collision rules as files.
    connection.execute(sa.text(
        "WITH ranked AS (SELECT i.id, ROW_NUMBER() OVER ("
        "PARTITION BY o.canonical_workspace_id, i.site_skill_id "
        "ORDER BY (i.workspace_id <> o.canonical_workspace_id), i.created_at, i.id) AS rank "
        "FROM workspace_skill_installs i JOIN personal_account_ownerships o USING (workspace_id)) "
        "DELETE FROM workspace_skill_installs i USING ranked WHERE i.id = ranked.id AND ranked.rank > 1"
    ))


def upgrade():
    connection = op.get_bind()
    missing = connection.scalars(sa.text(
        "SELECT w.id FROM workspaces w "
        "WHERE NOT EXISTS (SELECT 1 FROM memberships m "
        "  WHERE m.workspace_id = w.id AND m.role = 'admin') "
        "ORDER BY w.id"
    )).all()
    # A fresh install contains a reserved workspace and the default grant even
    # before any user exists. Leave that seed available for first registration.
    unresolved = [workspace_id for workspace_id in missing if not _unclaimed_seed(connection, workspace_id)]
    if unresolved:
        raise RuntimeError("Cannot migrate workspace ownership: no active administrator for "
                           + ", ".join(map(str, unresolved)))

    op.create_table(
        "personal_account_ownerships",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_by", sa.String(32), nullable=False, server_default="admin_created_at"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["canonical_workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_index(
        "ix_personal_account_ownerships_user_id",
        "personal_account_ownerships", ["user_id"],
    )
    op.create_index(
        "ix_personal_account_ownerships_canonical_workspace_id",
        "personal_account_ownerships", ["canonical_workspace_id"],
    )
    op.create_table(
        "user_credit_accounts",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.Numeric(24, 4), nullable=False, server_default="0"),
        sa.Column("lifetime_granted", sa.Numeric(24, 4), nullable=False, server_default="0"),
        sa.Column("lifetime_spent", sa.Numeric(24, 4), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "user_model_policies",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("available_models", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("default_model", sa.String(200), nullable=True),
        sa.Column("billing_multiplier_percent", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "billing_multiplier_percent BETWEEN 1 AND 10000",
            name="ck_user_model_policy_multiplier",
        ),
    )
    op.create_table(
        "account_cleanup_objects",
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("object_key"),
    )
    op.create_index(
        "ix_account_cleanup_objects_status", "account_cleanup_objects", ["status"]
    )
    op.alter_column("site_payment_configs", "created_by", existing_type=sa.Uuid(), nullable=True)
    for column in ("created_by", "updated_by"):
        op.alter_column("site_skills", column, existing_type=sa.Uuid(), nullable=True)

    # Rank receiving users first, then pick the oldest workspace actually won.
    connection.execute(sa.text(
        "WITH receivers AS ("
        "  SELECT DISTINCT ON (m.workspace_id) m.workspace_id, m.user_id, w.created_at "
        "  FROM memberships m JOIN users u ON u.id = m.user_id "
        "  JOIN workspaces w ON w.id = m.workspace_id "
        "  WHERE m.role = 'admin' "
        "  ORDER BY m.workspace_id, u.created_at, u.id"
        ") "
        "INSERT INTO personal_account_ownerships "
        "(workspace_id, user_id, canonical_workspace_id) "
        "SELECT workspace_id, user_id, "
        "FIRST_VALUE(workspace_id) OVER (PARTITION BY user_id ORDER BY created_at, workspace_id) "
        "FROM receivers"
    ))
    # Former non-receiving members still need personal accounts with an empty
    # resource namespace. Do not give them any of their old memberships.
    connection.execute(sa.text(
        "INSERT INTO workspaces (id, name) SELECT u.id, left(u.name, 100) FROM users u "
        "WHERE NOT u.is_suspended AND NOT EXISTS ("
        "  SELECT 1 FROM personal_account_ownerships o WHERE o.user_id = u.id"
        ")"
    ))
    connection.execute(sa.text(
        "INSERT INTO personal_account_ownerships (workspace_id, user_id, canonical_workspace_id) "
        "SELECT u.id, u.id, u.id FROM users u WHERE NOT u.is_suspended AND NOT EXISTS ("
        "  SELECT 1 FROM personal_account_ownerships o WHERE o.user_id = u.id"
        ")"
    ))

    op.add_column("knowledge_bases", sa.Column("owner_id", sa.Uuid(), nullable=True))
    op.add_column("workspace_directories", sa.Column("marker_key", sa.String(700), nullable=True))
    connection.execute(sa.text(
        "UPDATE workspace_directories SET marker_key = "
        "'workspaces/' || workspace_id::text || '/resource-directory/' || path || '/.rag-directory'"
    ))
    op.create_foreign_key("fk_knowledge_bases_owner_id", "knowledge_bases", "users", ["owner_id"], ["id"])
    op.create_index("ix_knowledge_bases_owner_id", "knowledge_bases", ["owner_id"])
    op.add_column("audit_logs", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_audit_logs_owner_user_id", "audit_logs", "users", ["owner_user_id"], ["id"]
    )
    op.create_index("ix_audit_logs_owner_user_id", "audit_logs", ["owner_user_id"])
    connection.execute(sa.text(
        "UPDATE audit_logs r SET owner_user_id = o.user_id "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
    ))
    for table in ("knowledge_bases", "conversations"):
        connection.execute(sa.text(
            f"UPDATE {table} r SET owner_id = o.user_id "
            "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
        ))
    connection.execute(sa.text(
        "UPDATE query_traces r SET owner_id = k.owner_id FROM knowledge_bases k "
        "WHERE r.knowledge_base_id = k.id AND k.owner_id IS NOT NULL"
    ))

    connection.execute(sa.text(
        "INSERT INTO user_credit_accounts "
        "(user_id, balance, lifetime_granted, lifetime_spent) "
        "SELECT o.user_id, SUM(COALESCE(c.balance, 0)), "
        "SUM(COALESCE(c.lifetime_granted, 0)), SUM(COALESCE(c.lifetime_spent, 0)) "
        "FROM personal_account_ownerships o "
        "LEFT JOIN credit_accounts c ON c.workspace_id = o.workspace_id "
        "GROUP BY o.user_id"
    ))

    # The old catalog allowed every workspace to select all enabled chat models.
    # Preserve that union and prefer the earliest workspace's active default.
    connection.execute(sa.text(
        "INSERT INTO user_model_policies "
        "(user_id, available_models, default_model, billing_multiplier_percent) "
        "SELECT users.id, models.available_models, "
        "COALESCE(defaults.default_model, models.site_default), "
        "COALESCE(defaults.billing_multiplier_percent, 100) "
        "FROM users "
        "CROSS JOIN ("
        "  SELECT COALESCE(json_agg(p.id::text ORDER BY p.id::text), '[]'::json) AS available_models, "
        "  MAX(p.id::text) FILTER (WHERE p.id = s.default_provider_id) AS site_default "
        "  FROM site_model_providers p "
        "  LEFT JOIN model_billing_settings s ON s.id = 1 "
        "  WHERE p.enabled = true AND p.supports_chat = true"
        ") models "
        "LEFT JOIN LATERAL ("
        "  SELECT p.id::text AS default_model, w.billing_multiplier_percent "
        "  FROM personal_account_ownerships o "
        "  JOIN workspaces w ON w.id = o.workspace_id "
        "  LEFT JOIN (llm_provider_configs b JOIN site_model_providers p "
        "    ON p.id = b.site_provider_id AND p.enabled = true AND p.supports_chat = true) "
        "    ON b.workspace_id = w.id AND b.active = true AND b.enabled = true "
        "  WHERE o.user_id = users.id AND o.workspace_id = o.canonical_workspace_id "
        "  ORDER BY w.created_at, w.id, b.created_at, b.id LIMIT 1"
        ") defaults ON true "
        "WHERE users.is_suspended = false"
    ))

    _resolve_merge_collisions(connection)

    # Keep the legacy workspace id only as an internal physical partition.
    # Rows that already carry a globally unique id can be moved in place; the
    # user-facing authorization boundary is the receiver recorded above.
    movable_tables = (
        "answer_feedback",
        "audit_logs",
        "billing_snapshots",
        "document_uploads",
        "model_thinking_tests",
        "usage_events",
        "model_usage_requests",
        "product_suggestions",
        "agent_skills",
        "mcp_servers",
        "workspace_execution_jobs",
        "storage_transfers",
        "question_workbench_grants",
        "conversation_document_saves",
        "recharge_orders",
        "workspace_file_versions",
        "chat_attachments",
        "resource_previews",
        "workspace_file_operations",
        "image_tasks",
        "remote_tool_executions",
        "cloud_computer_sessions",
        "skill_uploads",
        "knowledge_base_tool_executions",
        "workspace_skill_installs",
    )
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names())
    for table_name in movable_tables:
        if table_name not in existing:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "workspace_id" not in columns:
            continue
        connection.execute(sa.text(
            f"UPDATE {sa.sql.elements.quoted_name(table_name, quote=True)} r "
            "SET workspace_id = o.canonical_workspace_id "
            "FROM personal_account_ownerships o "
            "WHERE r.workspace_id = o.workspace_id "
            "AND r.workspace_id <> o.canonical_workspace_id"
        ))

    op.drop_column("login_sessions", "workspace_id")

    for table, column in (
        ("agent_skills", "owner_id"),
        ("mcp_servers", "owner_id"),
        ("remote_tool_executions", "owner_id"),
        ("chat_attachments", "user_id"),
        ("document_uploads", "user_id"),
        ("skill_uploads", "user_id"),
        ("cloud_computer_sessions", "user_id"),
        ("workspace_execution_jobs", "user_id"),
        ("recharge_orders", "user_id"),
        ("image_tasks", "actor_user_id"),
    ):
        if table in existing:
            connection.execute(sa.text(
                f"UPDATE {table} r SET {column} = o.user_id "
                "FROM personal_account_ownerships o "
                f"WHERE r.workspace_id = o.canonical_workspace_id AND r.{column} <> o.user_id"
            ))

    connection.execute(sa.text(
        "INSERT INTO workspace_storage_states (workspace_id, over_quota_since) "
        "SELECT o.canonical_workspace_id, MIN(s.over_quota_since) "
        "FROM workspace_storage_states s JOIN personal_account_ownerships o USING (workspace_id) "
        "GROUP BY o.canonical_workspace_id ON CONFLICT (workspace_id) DO UPDATE "
        "SET over_quota_since = EXCLUDED.over_quota_since"
    ))
    connection.execute(sa.text(
        "DELETE FROM workspace_storage_states s USING personal_account_ownerships o "
        "WHERE s.workspace_id = o.workspace_id AND o.workspace_id <> o.canonical_workspace_id"
    ))
    # Preserve entitlement IDs because existing transfers refer to them. The
    # secondary grants retain their original expiry and consumption counters.
    connection.execute(sa.text(
        "UPDATE storage_entitlements s SET workspace_id = o.canonical_workspace_id, "
        "request_key = CASE WHEN s.kind = 'free' OR EXISTS ("
        "  SELECT 1 FROM storage_entitlements other JOIN personal_account_ownerships mapped USING (workspace_id) "
        "  WHERE mapped.canonical_workspace_id = o.canonical_workspace_id AND other.id <> s.id "
        "    AND other.request_key = s.request_key"
        ") THEN 'migration:' || s.id::text ELSE s.request_key END "
        "FROM personal_account_ownerships o WHERE s.workspace_id = o.workspace_id "
        "AND s.workspace_id <> o.canonical_workspace_id"
    ))

    # Conversations and knowledge bases are the two resources queried on
    # nearly every request.  Their ownership is explicit and must follow the
    # receiver even when the historical owner field was null or stale.
    connection.execute(sa.text(
        "UPDATE conversations r SET workspace_id = o.canonical_workspace_id, owner_id = o.user_id "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
    ))
    connection.execute(sa.text(
        "UPDATE knowledge_bases r SET workspace_id = o.canonical_workspace_id, owner_id = o.user_id "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
    ))

    # Merge the physical credit accounts into the canonical partition.  The
    # ledger rows are historical facts: move their ownership partition only,
    # never re-price or recreate entries.
    connection.execute(sa.text(
        "UPDATE credit_accounts target SET "
        "balance = source.balance, lifetime_granted = source.lifetime_granted, "
        "lifetime_spent = source.lifetime_spent, updated_at = now() "
        "FROM user_credit_accounts source "
        "JOIN personal_account_ownerships canonical "
        "  ON canonical.user_id = source.user_id "
        " AND canonical.workspace_id = canonical.canonical_workspace_id "
        "WHERE target.workspace_id = canonical.workspace_id"
    ))
    connection.execute(sa.text(
        "DELETE FROM credit_accounts c USING personal_account_ownerships o "
        "WHERE c.workspace_id = o.workspace_id "
        "AND o.workspace_id <> o.canonical_workspace_id"
    ))
    connection.execute(sa.text(
        "UPDATE credit_ledger r SET workspace_id = o.canonical_workspace_id "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
    ))
    connection.execute(sa.text(
        "UPDATE usage_policies r SET workspace_id = o.canonical_workspace_id "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id "
        "AND r.provider_id IS NOT NULL"
    ))
    connection.execute(sa.text(
        "UPDATE llm_provider_configs r SET encrypted_api_key = '', base_url = '' "
        "FROM personal_account_ownerships o WHERE r.workspace_id = o.workspace_id"
    ))

    _merge_resource_paths(connection)


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM personal_account_ownerships) OR EXISTS (SELECT 1 FROM login_sessions) "
        "OR EXISTS (SELECT 1 FROM site_payment_configs WHERE created_by IS NULL) "
        "OR EXISTS (SELECT 1 FROM site_skills WHERE created_by IS NULL OR updated_by IS NULL)"
    )):
        raise RuntimeError("Restore a pre-migration database backup to reverse the ownership transfer.")
    op.add_column("login_sessions", sa.Column("workspace_id", sa.Uuid(), nullable=False))
    op.create_foreign_key("login_sessions_workspace_id_fkey", "login_sessions", "workspaces",
                          ["workspace_id"], ["id"])
    op.drop_index("ix_audit_logs_owner_user_id", table_name="audit_logs")
    op.drop_constraint("fk_audit_logs_owner_user_id", "audit_logs", type_="foreignkey")
    op.drop_column("audit_logs", "owner_user_id")
    op.drop_index("ix_knowledge_bases_owner_id", table_name="knowledge_bases")
    op.drop_constraint("fk_knowledge_bases_owner_id", "knowledge_bases", type_="foreignkey")
    op.drop_column("knowledge_bases", "owner_id")
    op.drop_column("workspace_directories", "marker_key")
    op.drop_index("ix_account_cleanup_objects_status", table_name="account_cleanup_objects")
    op.drop_table("account_cleanup_objects")
    op.alter_column("site_payment_configs", "created_by", existing_type=sa.Uuid(), nullable=False)
    for column in ("created_by", "updated_by"):
        op.alter_column("site_skills", column, existing_type=sa.Uuid(), nullable=False)
    op.drop_table("user_model_policies")
    op.drop_table("user_credit_accounts")
    op.drop_index(
        "ix_personal_account_ownerships_canonical_workspace_id",
        table_name="personal_account_ownerships",
    )
    op.drop_index(
        "ix_personal_account_ownerships_user_id",
        table_name="personal_account_ownerships",
    )
    op.drop_table("personal_account_ownerships")
