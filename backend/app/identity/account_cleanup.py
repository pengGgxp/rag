"""清理停用账号，并持久化待删除对象队列。"""

from collections.abc import Callable, Iterable
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from .. import storage_models  # noqa: F401
from ..models import (
    AccountCleanupObject, Base, CloudComputerSession, DocumentUpload,
    LegacyAccountObjectPrefix, SkillUpload, User,
)


_AUDIT_TABLES = {"audit_logs", "site_audit_events", "site_auth_audit"}
_HISTORICAL_ACTORS = {"actor_user_id", "created_by", "updated_by", "handled_by"}
_ACTOR_OWNED_TABLES = {"image_tasks", "model_thinking_tests"}
_OBJECT_COLUMNS = {"object_key", "marker_key", "mounted_object_key", "mounted_marker_key"}


def _cleanup_scope(db, user_ids, prefixes, tables):
    predicates = {}
    historical = []
    tables_by_name = {table.name: table for table in tables}
    for table in tables:
        if table.name in {"users", "account_cleanup_objects"}:
            continue
        clauses = []
        for fk in table.foreign_keys:
            parent, column = fk.column.table, fk.parent
            if parent.name == "users":
                if (column.name in _HISTORICAL_ACTORS and table.name not in _AUDIT_TABLES
                        and table.name not in _ACTOR_OWNED_TABLES):
                    historical.append((table, column))
                else:
                    clauses.append(column.in_(user_ids))
            elif parent.name in predicates and fk.ondelete != "SET NULL":
                clauses.append(column.in_(select(fk.column).where(predicates[parent.name])))
        if table.name == "authentication_flows":
            clauses.append(table.c.email.in_(select(User.email).where(User.id.in_(user_ids))))
        if table.name == "site_audit_events":
            legacy_ids = [prefix.split("/")[1] for prefix in prefixes if prefix.startswith("workspaces/")]
            clauses.append(table.c.target_id.in_([*(str(value) for value in user_ids), *legacy_ids]))
        if table.name == "resource_previews" and "workspace_files" in predicates:
            files = tables_by_name["workspace_files"]
            clauses.append(table.c.resource_id.in_(select(files.c.id).where(predicates[files.name])))
        if clauses:
            predicates[table.name] = or_(*clauses)

    for table, column in historical:
        kept = column.in_(user_ids)
        if table.name in predicates:
            kept = kept & ~func.coalesce(predicates[table.name], False)
        if column.nullable:
            db.execute(update(table).where(kept).values({column: None}))
        elif "account_id" in table.c:
            db.execute(update(table).where(kept).values({column: table.c.account_id}))
    return predicates


def _queue_objects(db, prefixes, tables, predicates, list_objects):
    keys = set()
    references = [
        (table, column) for table in tables
        if table.name not in {"account_cleanup_objects", "storage_migration_objects"}
        for column in table.c if column.name in _OBJECT_COLUMNS
    ]
    for table, column in references:
        if table.name in predicates:
            keys.update(db.scalars(select(column).where(predicates[table.name], column.is_not(None))))
    if list_objects:
        for prefix in sorted(prefixes):
            keys.update(key for key in list_objects(prefix) if key.startswith(prefix))
    for table, column in references:
        if not keys:
            break
        retained = select(column).where(column.in_(keys))
        if table.name in predicates:
            retained = retained.where(~func.coalesce(predicates[table.name], False))
        keys.difference_update(db.scalars(retained))
    for key in sorted(keys):
        if db.get(AccountCleanupObject, key) is None:
            db.add(AccountCleanupObject(object_key=key))
    return len(keys)


def cleanup_external_resources(db, user_ids):
    """Revoke resources that cannot be removed by deleting an object key."""
    from app.cloud_computer.adapter import adapter
    from app.files.cache import purge_account_cache
    from app.infra.storage_backend import current_storage

    for row in db.scalars(select(CloudComputerSession).where(
        CloudComputerSession.account_id.in_(user_ids), CloudComputerSession.ended_at.is_(None)
    )):
        provider = adapter(row.snapshot, row.encrypted_api_key)
        identifiers = {identifier for identifier, _ in provider.find(row.id)}
        if row.provider_id:
            identifiers.add(row.provider_id)
        for identifier in sorted(identifiers):
            if provider.exists(identifier) and not provider.kill(identifier):
                raise RuntimeError(f"Could not stop cloud computer {row.id}; retry cleanup.")

    uploads = [
        row for model in (DocumentUpload, SkillUpload)
        for row in db.scalars(select(model).where(
            model.state.in_(("uploading", "completing")),
            or_(model.account_id.in_(user_ids), model.user_id.in_(user_ids)),
        ))
    ]
    if uploads:
        storage = current_storage(db)
        for upload in uploads:
            storage.abort(upload.object_key, upload.multipart_id)
    purge_account_cache(user_ids)


def cleanup_suspended_accounts(
    db: Session,
    *,
    batch_size: int = 100,
    delete_object: Callable[[str], None] | None = None,
    list_objects: Callable[[str], Iterable[str]] | None = None,
    cleanup_external: Callable[[Session, list[UUID]], None] | None = None,
) -> dict:
    """Commit database deletes and their object work list atomically."""
    suspended: list[UUID] = list(db.scalars(
        select(User.id).where(User.is_suspended.is_(True))
        .order_by(User.created_at, User.id).limit(batch_size).with_for_update()
    ))
    queued = 0
    if suspended:
        if cleanup_external is not None:
            cleanup_external(db, suspended)
        prefixes = set(db.scalars(select(LegacyAccountObjectPrefix.prefix).where(
            LegacyAccountObjectPrefix.user_id.in_(suspended)
        )))
        prefixes.update(f"accounts/{identifier}/" for identifier in suspended)
        tables = Base.metadata.sorted_tables
        predicates = _cleanup_scope(db, suspended, prefixes, tables)
        queued = _queue_objects(db, prefixes, tables, predicates, list_objects)
        for table in reversed(tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.execute(delete(User).where(User.id.in_(suspended)))
    db.commit()

    if delete_object is None:
        from app.infra.storage import delete_object
    pending = db.scalars(
        select(AccountCleanupObject).order_by(AccountCleanupObject.attempts,
            AccountCleanupObject.created_at, AccountCleanupObject.object_key)
        .limit(batch_size * 20).with_for_update(skip_locked=True)
    ).all()
    deleted, failed = [], []
    for row in pending:
        row.attempts += 1
        try:
            delete_object(row.object_key)
        except Exception as exc:
            row.status = "failed"
            row.last_error = str(exc)[:500]
            failed.append({"object_key": row.object_key, "error": row.last_error})
        else:
            deleted.append(row.object_key)
            db.delete(row)
    db.commit()
    return {
        "accounts_deleted": len(suspended),
        "objects_queued": queued,
        "objects_deleted": deleted,
        "objects_failed": failed,
        "remaining_objects": list(db.scalars(select(AccountCleanupObject.object_key)
            .order_by(AccountCleanupObject.object_key))),
    }
