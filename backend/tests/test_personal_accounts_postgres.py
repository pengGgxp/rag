"""Run the account ownership migration from revision 0078 on PostgreSQL."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import (
    AuditLog, BillingSnapshot, CreditLedgerEntry, KnowledgeBase,
    LegacyAccountObjectPrefix, LoginSession, User, UserCreditAccount,
    UserModelPolicy, WorkspaceFile, WorkspaceFileVersion,
)
from app.personal_accounts import cleanup_suspended_accounts
from app.storage_models import StorageEntitlement


@pytest.fixture
def legacy_database():
    url = os.environ.get("PERSONAL_ACCOUNTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL database migrated to 0078")
    if not (make_url(url).database or "").startswith("rag_auth_test_"):
        pytest.fail("refusing a non-test database")
    engine = create_engine(url)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            context = MigrationContext.configure(connection)
            assert context.get_current_revision() == "0078_tool_detail"
            with Session(connection) as db:
                def migrate():
                    with Operations.context(context):
                        for name in ("0079_personal_account_ownership", "0080_user_resources",
                                     "0081_direct_account_ownership"):
                            path = Path(__file__).parents[1] / "alembic" / "versions" / f"{name}.py"
                            spec = spec_from_file_location(name, path)
                            revision = module_from_spec(spec)
                            spec.loader.exec_module(revision)
                            revision.upgrade()
                try:
                    yield db, migrate
                finally:
                    transaction.rollback()
    finally:
        engine.dispose()


def insert(db, table_name, **values):
    metadata = db.info.setdefault("legacy_metadata", MetaData())
    table = Table(table_name, metadata, autoload_with=db.connection(), resolve_fks=False)
    if "id" in table.c and "id" not in values:
        values["id"] = uuid4()
    db.execute(table.insert().values(**values))
    return values.get("id")


def account(db, name, **values):
    return insert(db, "users", email=f"{name}@example.com", name=name, **values)


def workspace(db, name, admin, **values):
    workspace_id = insert(db, "workspaces", name=name, **values)
    insert(db, "memberships", workspace_id=workspace_id, user_id=admin, role="admin")
    return workspace_id


def old_file(db, workspace_id, actor, path):
    file_id = insert(db, "workspace_files", workspace_id=workspace_id, path=path,
        content_type="text/plain", size_bytes=1, content_hash="old", current_version=1,
        created_by=actor, updated_by=actor)
    key = f"workspaces/{workspace_id}/resource-directory/{path}"
    insert(db, "workspace_file_versions", file_id=file_id, workspace_id=workspace_id,
        version=1, object_key=key, content_type="text/plain", size_bytes=1,
        content_hash="old", created_by=actor)
    return file_id, key


def test_fresh_upgrade_removes_workspace_schema_and_unclaimed_seed(legacy_database):
    db, migrate = legacy_database
    migrate()
    inspector = inspect(db.connection())
    assert {"workspaces", "memberships", "credit_accounts", "library_grants",
            "personal_account_ownerships"}.isdisjoint(inspector.get_table_names())
    for table in inspector.get_table_names():
        assert "workspace_id" not in {column["name"] for column in inspector.get_columns(table)}
    assert db.scalars(select(CreditLedgerEntry)).all() == []


def test_orphaned_workspace_fails_before_schema_changes(legacy_database):
    db, migrate = legacy_database
    orphan = insert(db, "workspaces", name="No administrator")
    with pytest.raises(RuntimeError, match=str(orphan)):
        migrate()
    assert not inspect(db.connection()).has_table("personal_account_ownerships")


def test_merge_preserves_billing_model_policy_and_physical_keys(legacy_database):
    db, migrate = legacy_database
    time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    owner = account(db, "Owner", id=UUID(int=1), created_at=time)
    other = account(db, "Other", id=UUID(int=2), created_at=time)
    before = workspace(db, "Before", owner, created_at=time, billing_multiplier_percent=125)
    after = workspace(db, "After", owner, created_at=time.replace(month=2), billing_multiplier_percent=175)
    insert(db, "memberships", workspace_id=before, user_id=other, role="admin")

    file_a, key_a = old_file(db, before, other, "report.txt")
    file_b, key_b = old_file(db, after, owner, "report.txt")
    old_file(db, before, owner, "report (2).txt")
    library_id = insert(db, "knowledge_bases", workspace_id=after, name="Historical library",
                        status="DRAFT", restricted=True, tags=[])
    insert(db, "login_sessions", token_hash="session", user_id=other, workspace_id=before,
           csrf_token="csrf", expires_at=time + timedelta(days=999))
    connection = insert(db, "model_connections", name="Shared connection", base_url="https://example.invalid",
                        platform="custom", encrypted_api_key="site-only", timeout_seconds=30, enabled=True, version=1)
    model_ids, charge_ids = [], []
    request_id = uuid4()
    for index, scope in enumerate((before, after), 1):
        model = insert(db, "site_model_providers", key=f"model-{index}", name="Model",
                       model=f"model-{index}", connection_id=connection, enabled=True, supports_chat=True,
                       published=True, version=1, prices={})
        model_ids.append(str(model))
        insert(db, "llm_provider_configs", workspace_id=scope, site_provider_id=model,
               key="same", name="Shared", base_url="", model="example", encrypted_api_key="old", active=True)
        insert(db, "credit_accounts", workspace_id=scope, balance=Decimal("12.3456") * index,
               lifetime_granted=20 * index, lifetime_spent=Decimal("7.6544") * index)
        charge_ids.append(insert(db, "credit_ledger", workspace_id=scope, amount=Decimal("-7.6544") * index,
               balance_after=Decimal("12.3456") * index, kind="usage", idempotency_key=f"charge-{index}", metadata={}))
        insert(db, "billing_snapshots", workspace_id=scope, request_id=request_id, event_key="same",
               context={"multiplier_percent": index * 100}, status="complete")
        insert(db, "storage_entitlements", workspace_id=scope, request_key="same", kind="package",
               name="Quota", storage_bytes=100 * index, download_bytes=200 * index, used_bytes=index,
               reserved_bytes=0, price_credits=1, snapshot={}, expires_at=time + timedelta(days=999))
    unbound_model = insert(db, "site_model_providers", key="unbound", name="Unbound", model="unbound",
        connection_id=connection, enabled=True, supports_chat=True, published=True, version=1, prices={})
    settings = Table("model_billing_settings", db.info["legacy_metadata"],
        autoload_with=db.connection(), resolve_fks=False)
    if db.scalar(select(settings.c.id).where(settings.c.id == 1)) is None:
        db.execute(settings.insert().values(id=1, default_provider_id=unbound_model))
    else:
        db.execute(settings.update().where(settings.c.id == 1).values(default_provider_id=unbound_model))

    migrate()
    assert db.get(WorkspaceFile, file_a).owner_user_id == owner
    assert db.get(WorkspaceFile, file_b).path == "report (3).txt"
    assert {row.file_id: (row.account_id, row.object_key) for row in db.scalars(
        select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id.in_((file_a, file_b)))
    )} == {file_a: (owner, key_a), file_b: (owner, key_b)}
    assert db.get(KnowledgeBase, library_id).account_id == owner
    assert db.get(LoginSession, "session").user_id == other
    assert db.get(User, owner).is_site_admin is False
    assert db.get(UserCreditAccount, owner).balance == Decimal("37.0368")
    policy = db.get(UserModelPolicy, owner)
    assert policy.default_model == model_ids[0]
    assert set(policy.available_models) == {*model_ids, str(unbound_model)}
    assert db.get(UserModelPolicy, other).default_model == str(unbound_model)
    assert policy.billing_multiplier_percent == 125
    assert [db.get(CreditLedgerEntry, item).amount for item in charge_ids] == [
        Decimal("-7.6544"), Decimal("-15.3088")]
    grants = db.scalars(select(StorageEntitlement).where(StorageEntitlement.account_id == owner)).all()
    assert sum(row.storage_bytes for row in grants) == 300
    assert sum(row.download_bytes for row in grants) == 600
    assert len({row.request_key for row in grants}) == 2
    snapshots = db.scalars(select(BillingSnapshot).where(BillingSnapshot.request_id == request_id)).all()
    assert {row.account_id for row in snapshots} == {owner}
    assert {row.context["multiplier_percent"] for row in snapshots} == {100, 200}


def test_suspended_cleanup_retries_objects_and_preserves_other_owner(legacy_database):
    db, migrate = legacy_database
    normal = account(db, "Normal")
    suspended = account(db, "Suspended", is_suspended=True)
    kept = workspace(db, "Keep", normal)
    discarded = workspace(db, "Discard", suspended)
    insert(db, "memberships", workspace_id=kept, user_id=suspended, role="editor")
    keep_file, keep_key = old_file(db, kept, suspended, "keep.txt")
    discard_file, discard_key = old_file(db, discarded, suspended, "delete.txt")
    keep_audit = insert(db, "audit_logs", workspace_id=kept, actor_user_id=normal,
                        action="file.read", resource_type="file", metadata={})
    insert(db, "audit_logs", workspace_id=kept, actor_user_id=suspended,
           action="file.read", resource_type="file", metadata={})
    migrate()
    prefix = f"workspaces/{discarded}/"
    retry_key = prefix + "unindexed.txt"
    assert db.get(LegacyAccountObjectPrefix, prefix).user_id == suspended
    deleted = []

    def remove(key):
        if key == retry_key:
            raise OSError("temporary storage failure")
        deleted.append(key)

    first = cleanup_suspended_accounts(db, delete_object=remove,
        list_objects=lambda key: [retry_key, keep_key] if key == prefix else [])
    assert first["accounts_deleted"] == 1
    assert first["remaining_objects"] == [retry_key]
    assert deleted == [discard_key]
    assert db.get(User, suspended) is None
    assert db.get(WorkspaceFile, discard_file) is None
    assert db.get(WorkspaceFile, keep_file).updated_by == normal
    assert db.scalars(select(AuditLog.id)).all() == [keep_audit]
    retry = cleanup_suspended_accounts(db, delete_object=deleted.append)
    assert retry["remaining_objects"] == []
    assert deleted == [discard_key, retry_key]
