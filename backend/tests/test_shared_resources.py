import hashlib
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import cloud_computer as cloud, shared_resources as shared, workspace_files
from app.models import WorkspaceFile, WorkspaceFileVersion
from test_cloud_computer import setup, submit
from test_cloud_computer_mount import MemoryMount, configure_mount
from test_resources import resource_store, upload


def mounted(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    return client, db, conversation, access, storage


def test_changes_use_server_copy_without_body_download_or_cache(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    original_key = db.scalar(select(WorkspaceFileVersion.object_key))
    key = shared.prefix(access.workspace_id) + item["path"]
    row = db.get(cloud.Computer, submit(db, access, conversation).session_id)
    cloud.stage_mount(db, row, access, [])
    storage.objects[key] = b"cloud modification"
    monkeypatch.setattr(storage, "download", lambda *_: pytest.fail("body downloaded"))
    monkeypatch.setattr("app.resource_cache.read_bytes", lambda *_: pytest.fail("download quota used"))
    provider = SimpleNamespace(manifest=lambda _sandbox: {item["path"]: {
        "sha256": hashlib.sha256(b"cloud modification").hexdigest(), "size": len(b"cloud modification")}})
    files = cloud.synchronize(db, row, provider, None)
    assert files[0]["version"] == 2
    assert storage.objects[original_key] == b"resource facts"
    assert not resource_store[1]
    assert cloud.synchronize(db, row, provider, None) == []
    cloud.cleanup_mount(db, row)
    assert storage.objects[key] == b"cloud modification"


def test_filename_recreation_does_not_revive_deleted_resource(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    attached = client.post(f"/resources/{item['id']}/attachment?version=1").json()
    assert client.delete(f"/resources/{item['id']}").status_code == 204
    storage.objects[shared.prefix(access.workspace_id) + item["path"]] = b"new file"
    files = shared.recover(db, access, storage)
    assert files[0]["id"] != item["id"]
    assert files[0]["version"] == 1
    assert client.get(f"/resources/{item['id']}").status_code == 404
    assert client.get(f"/chat/attachments/{attached['id']}/content").status_code == 404


def test_external_deletion_updates_resource_and_preserves_old_object(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    original_key = db.scalar(select(WorkspaceFileVersion.object_key))
    storage.delete(shared.prefix(access.workspace_id) + item["path"])
    assert shared.recover(db, access, storage) == []
    assert client.get(f"/resources/{item['id']}").status_code == 404
    assert storage.objects[original_key] == b"resource facts"


def test_migration_resumes_without_recopy_or_download(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    item = upload(client)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    monkeypatch.setattr(storage, "download", lambda *_: pytest.fail("migration downloaded bytes"))
    assert shared.migrate(db) == 1
    assert shared.migrate(db) == 0
    assert len(storage.copies) == 1
    row = db.get(WorkspaceFile, UUID(item["id"]))
    assert row.live_etag
    assert storage.objects[shared.prefix(access.workspace_id) + item["path"]] == b"resource facts"


def test_concurrent_change_cannot_register_stale_hash(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    before = shared.objects(storage, access.workspace_id)
    storage.objects[shared.prefix(access.workspace_id) + item["path"]] = b"changed concurrently"
    with pytest.raises(HTTPException) as error:
        shared.snapshot(db, access, storage, {item["path"]: {
            "sha256": item["sha256"], "size": item["size_bytes"]}}, before)
    assert error.value.status_code == 409
    assert db.get(WorkspaceFile, UUID(item["id"])).current_version == 1


def test_api_write_is_immediately_visible_without_another_command(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    result = workspace_files._save_bytes(access.workspace_id, access, db, item["path"],
        b"API edit", "text/plain", expected_version=1)
    assert result["version"] == 2
    assert storage.objects[shared.prefix(access.workspace_id) + item["path"]] == b"API edit"
    assert not resource_store[1]
