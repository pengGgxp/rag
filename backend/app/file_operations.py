"""Generic workspace writes and short-lived compensation materials.

API writes serialize across workers. OSS/mounted external writes do not join
this lock: revision checks and guarded compensation are best effort (choice A).
No temporary object is under the mounted resource-directory prefix.
"""
import hashlib
import io
import json
import threading
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm.attributes import flag_modified

from .models import WorkspaceFileOperation
from . import shared_resources as shared

_locks = [threading.RLock() for _ in range(97)]
_local = threading.local()


@contextmanager
def workspace_lock(db, workspace_id):
    """Keep a session advisory lock across journal commits; reentrant per thread."""
    identity = str(workspace_id)
    held = getattr(_local, "held", set())
    if identity in held:
        yield
        return
    number = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
    with _locks[number % len(_locks)]:
        connection = None
        try:
            if db.bind.dialect.name == "postgresql":
                connection = db.bind.connect()
                connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": number})
            _local.held = held | {identity}
            yield
        finally:
            _local.held = held
            if connection is not None:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": number})
                finally:
                    connection.close()


def storage(db):
    value = shared.backend(db)
    if value is None:
        raise HTTPException(503, "工作区暂时无法读取或保存，请稍后重试")
    return value


def revision(metadata):
    if not metadata:
        return None
    # ETags are treated as opaque; no assumption that they are content MD5s.
    value = [metadata.get("ETag"), metadata.get("Content-Length", metadata.get("Size"))]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def file_revision(row):
    return revision({"ETag": row.live_etag or row.content_hash, "Content-Length": row.size_bytes})


def digest_object(store, key):
    digest = hashlib.sha256()
    with store.download(key) as body:
        for block in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_object(store, source, target, *, etag=None, create=False):
    # Every production mount adapter implements copy. Test adapters have the
    # same source guard; only OSS implements server-side forbid-overwrite.
    if getattr(store, "conditional_create", False):
        return store.copy(source, target, etag=etag, create=create)
    if create and store.head(target):
        raise HTTPException(409, "目标已存在，请选择保留两份或重新加载")
    return store.copy(source, target, etag=etag)


def _cleanup(store, operation):
    for step in operation.steps:
        for key in (step.get("backup"), step.get("staged")):
            if key:
                store.delete(key)


def restore(db, store, operation):
    """Never blindly replay deletion or overwrite a newer external object."""
    failed = False
    for step in reversed(operation.steps):
        if step.get("restored"):
            continue
        try:
            current = store.head(step["key"])
            if step.get("before") and revision(current) == step["before"]:
                pass  # This step was not applied, or has already been restored.
            elif current and step.get("delete"):
                raise RuntimeError("外部文件已变化")
            elif current and digest_object(store, step["key"]) != step.get("after_hash"):
                raise RuntimeError("外部文件已变化")
            elif step.get("backup"):
                copy_object(store, step["backup"], step["key"], create=current is None)
            elif current:
                store.delete(step["key"])
            step["restored"] = True
            operation.steps = [dict(item) for item in operation.steps]
            flag_modified(operation, "steps")
            db.commit()
        except Exception:
            db.rollback()
            failed = True
    operation.state = "recovery_required" if failed else "rolled_back"
    operation.error = "文件已变化或恢复未完成，请稍后重试；持续失败请联系管理员" if failed else None
    db.commit()
    if not failed:
        try:
            _cleanup(store, operation)
        except Exception:
            pass  # The maintenance task also cleans terminal-operation materials.
    return not failed


def replay(db, access, request_id, fingerprint):
    if not request_id:
        return None
    operation = db.scalar(select(WorkspaceFileOperation).where(
        WorkspaceFileOperation.workspace_id == access.workspace_id,
        WorkspaceFileOperation.request_id == request_id))
    if operation is None:
        return None
    if operation.fingerprint != fingerprint:
        raise HTTPException(409, "请求标识已用于其他操作，请重新发起")
    if operation.state == "completed":
        return operation.result
    if operation.state in ("pending", "recovery_required"):
        if not restore(db, storage(db), operation):
            raise HTTPException(409, "上次操作尚未恢复，请保留当前文件并稍后重试")
    # A compensated request may retry with the original ID.
    db.delete(operation)
    db.commit()
    return None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def execute(db, access, changes, update_metadata, *, request_id=None, signature=None):
    """Apply a fully preflighted batch. changes: key, data/source/delete, type.

    Durable intent precedes every live mutation. Metadata and completed state
    commit together, so interrupted operations either finalize or compensate.
    Caller must hold workspace_lock and check permissions/capacity before here.
    """
    access.editor()
    store = storage(db)
    signature = signature or fingerprint([{k: v for k, v in item.items() if k != "data"} |
        ({"hash": hashlib.sha256(item["data"]).hexdigest()} if "data" in item else {}) for item in changes])
    previous = replay(db, access, request_id, signature)
    if previous is not None:
        return previous
    targets = {item["key"] for item in changes} | {item["source"] for item in changes if item.get("source")}
    pending = db.scalars(select(WorkspaceFileOperation).where(
        WorkspaceFileOperation.workspace_id == access.workspace_id,
        WorkspaceFileOperation.state.in_(("pending", "recovery_required")))).all()
    for other in pending:
        if any(step["key"] in targets for step in other.steps):
            # Do not let later compensation undo a new write to the same file.
            raise HTTPException(409, "目标文件有尚未完成的操作，请先核对该文件的操作结果")
    identifier = uuid4()
    root = f"workspaces/{access.workspace_id}/file-operations/{identifier}/"
    operation = WorkspaceFileOperation(id=identifier, workspace_id=access.workspace_id, user_id=access.user_id,
        request_id=request_id or str(identifier), fingerprint=signature, state="pending", steps=[], result={})
    # Persist an empty intent before preparing materials, so crashes during
    # staging also have a durable owner and can be cleaned without live changes.
    db.add(operation)
    db.commit()
    try:
        for index, change in enumerate(changes):
            key, kind = change["key"], change.get("content_type", "application/octet-stream")
            before = store.head(key)
            if "expected" in change and change["expected"] != revision(before):
                raise HTTPException(409, "文件已变化，请重新加载后再操作")
            step = {"key": key, "before": revision(before), "delete": bool(change.get("delete")),
                    "backup": root + f"{index}.before" if before else None,
                    "staged": None if change.get("delete") else root + f"{index}.after"}
            operation.steps = [*operation.steps, step]
            db.commit()
            if before:
                copy_object(store, key, step["backup"], etag=before.get("ETag"), create=True)
            if not step["delete"]:
                if "data" in change:
                    store.put(step["staged"], io.BytesIO(change["data"]), kind)
                    step["after_hash"] = hashlib.sha256(change["data"]).hexdigest()
                else:
                    source = store.head(change["source"])
                    if not source or (change.get("source_revision") and revision(source) != change["source_revision"]):
                        raise HTTPException(409, "来源文件已变化，请刷新后重试")
                    copy_object(store, change["source"], step["staged"], etag=source.get("ETag"), create=True)
                    step["after_hash"] = digest_object(store, step["staged"])
                step["staged_etag"] = store.head(step["staged"]).get("ETag")
            step["prepared"] = True
            operation.steps = [dict(item) for item in operation.steps[:-1]] + [step]
            flag_modified(operation, "steps")
            db.commit()
        for step in operation.steps:
            if revision(store.head(step["key"])) != step["before"]:
                raise HTTPException(409, "文件已变化，请刷新后重试")
            if step["delete"]:
                store.delete(step["key"])
            else:
                copy_object(store, step["staged"], step["key"], etag=step.get("staged_etag"), create=step["before"] is None)
        result = update_metadata()
        operation.state, operation.result = "completed", result or {}
        db.commit()
    except BaseException:
        db.rollback()
        # Process death is recovered by maintenance; ordinary failures compensate
        # before returning. Do not turn failed compensation into a success.
        if not restore(db, store, operation):
            raise HTTPException(503, "操作未完成且文件恢复待处理，请稍后重试，勿重复覆盖") from None
        raise
    try:
        _cleanup(store, operation)
    except Exception:
        pass
    return operation.result


def recover_pending(db):
    identifiers = list(db.scalars(select(WorkspaceFileOperation.id).where(
        WorkspaceFileOperation.state.in_(("pending", "recovery_required", "completed", "rolled_back")))))
    for identifier in identifiers:
        operation = db.get(WorkspaceFileOperation, identifier)
        with workspace_lock(db, operation.workspace_id):
            db.refresh(operation)
            store = storage(db)
            if operation.state in ("pending", "recovery_required"):
                restore(db, store, operation)
            else:
                try:
                    _cleanup(store, operation)
                    operation.steps = []
                    db.commit()
                except Exception:
                    db.rollback()
                    continue
