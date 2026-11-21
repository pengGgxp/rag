from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import now
from app.models import AuditLog, CloudComputerSession, LegacyAccountObjectPrefix, User, WorkspaceFile
from app.personal_accounts import cleanup_suspended_accounts
from app.shared_resources import materialize_legacy_mount, object_key
from test_auth import library, register
from test_resources import resource_store, upload


def test_same_resource_names_do_not_cross_accounts(accounts, resource_store):
    (owner, another, _), db = accounts
    first = UUID(register(owner)["user"]["id"])
    second = UUID(register(another, "second@example.com")["user"]["id"])
    original = upload(owner, "report.txt", b"first owner")
    duplicate = upload(another, "report.txt", b"second owner")
    for client, own, other, content in (
        (owner, original, duplicate, b"first owner"),
        (another, duplicate, original, b"second owner"),
    ):
        assert own["path"] == other["path"]
        assert client.get(own["download_url"]).content == content
        assert client.get(other["download_url"]).status_code == 404
        assert client.delete(f"/resources/{other['id']}").status_code == 404
    assert library(owner) != library(another)
    for client in (owner, another):
        assert client.post("/conversations", json={"title": "Same conversation"}).status_code == 201
    assert {row.owner_user_id for row in db.scalars(select(WorkspaceFile))} == {first, second}


def test_legacy_mount_retry_preserves_original_key_and_resolves_file_path(accounts, resource_store):
    (owner, _, _), db = accounts
    user_id = UUID(register(owner)["user"]["id"])
    item = upload(owner, "report (2).txt", b"legacy bytes")
    file = db.get(WorkspaceFile, UUID(item["id"]))
    version = file.versions[0]
    target = object_key(user_id, file.path)
    prefix = "workspaces/00000000-0000-4000-a000-000000000001/"
    source = prefix + "resource-directory/report.txt"
    objects, _ = resource_store
    objects[source] = objects.pop(target)
    version.object_key = source
    db.add(LegacyAccountObjectPrefix(prefix=prefix, user_id=user_id))
    conversation = owner.post("/conversations", json={"title": "Legacy mount"}).json()
    computer = CloudComputerSession(account_id=user_id, user_id=user_id,
        conversation_id=UUID(conversation["id"]), session_hash="legacy", status="ready",
        encrypted_api_key="", snapshot={"resource_mount": {"prefix": prefix + "resource-directory/"}})
    db.add(computer)
    db.commit()
    assert owner.get(item["download_url"]).content == b"legacy bytes"
    with pytest.raises(HTTPException) as blocked:
        materialize_legacy_mount(db, user_id)
    assert blocked.value.status_code == 409 and target not in objects
    computer.ended_at = now()
    db.commit()
    materialize_legacy_mount(db, user_id)
    db.rollback()
    assert objects[target] == objects[source]
    materialize_legacy_mount(db, user_id)
    db.commit()
    assert version.object_key == source and version.storage_key == target
    objects[target] = b"cloud updated"
    assert owner.get(item["download_url"]).content == b"cloud updated"


def test_cleanup_preserves_database_for_retry_when_external_cleanup_fails(accounts):
    (owner, _, _), db = accounts
    user_id = UUID(register(owner)["user"]["id"])
    db.get(User, user_id).is_suspended = True
    db.commit()

    def fail(_db, ids):
        assert ids == [user_id]
        raise OSError("temporary upstream failure")

    with pytest.raises(OSError, match="temporary upstream failure"):
        cleanup_suspended_accounts(db, delete_object=lambda _key: None, cleanup_external=fail)
    db.rollback()
    assert db.get(User, user_id) is not None
    assert db.scalars(select(AuditLog).where(AuditLog.owner_user_id == user_id)).first() is not None
    result = cleanup_suspended_accounts(db, delete_object=lambda _key: None)
    assert result["accounts_deleted"] == 1
    assert db.get(User, user_id) is None
    assert db.scalars(select(AuditLog).where(AuditLog.owner_user_id == user_id)).all() == []
