import pytest
from uuid import UUID
from sqlalchemy import select

from app import cloud_computer as cloud, shared_resources as shared, workspace_files
from app.models import WorkspaceFileVersion
from test_cloud_computer import setup, submit
from test_cloud_computer_mount import MemoryMount, configure_mount
from test_resources import resource_store, upload


def mounted(accounts, monkeypatch, resource_store):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    return client, db, conversation, access, storage


def test_shell_edit_is_current_file_without_copy_or_body_read(accounts, monkeypatch, resource_store):
    client, db, conversation, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    storage.copies.clear()
    key = shared.object_key(access.user_id, item["path"])
    computer = db.get(cloud.Computer, submit(db, access, conversation).session_id)
    storage.objects[key] = b"cloud modification"
    monkeypatch.setattr(storage, "download", lambda *_: pytest.fail("body downloaded"))
    monkeypatch.setattr("app.resource_cache.read_bytes", lambda *_: pytest.fail("cache used"))
    assert cloud.synchronize(db, computer, None, None) == []
    result = client.get("/resources").json()["items"]
    assert result[0]["size_bytes"] == len(b"cloud modification")
    assert client.get(f"/resources/{item['id']}/download").content == b"cloud modification"
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1
    assert not storage.copies
    cloud.cleanup_mount(db, computer)
    assert storage.objects[key] == b"cloud modification"


def test_recreation_keeps_deleted_chat_reference_unavailable(accounts, monkeypatch, resource_store):
    client, db, _, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    attached = client.post(f"/resources/{item['id']}/attachment").json()
    assert client.delete(f"/resources/{item['id']}").status_code == 204
    storage.objects[shared.object_key(access.user_id, item["path"])] = b"new file"
    files = client.get("/resources").json()["items"]
    assert files[0]["id"] != item["id"]
    assert client.get(f"/resources/{item['id']}").status_code == 404
    assert client.get(f"/chat/attachments/{attached['id']}/content").status_code == 404
    from app import tasks
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(tasks, "SessionLocal", sessionmaker(bind=db.bind))
    tasks.cleanup_workspace_files()
    assert storage.objects[shared.object_key(access.user_id, item["path"])] == b"new file"


def test_shell_delete_invalidates_old_links(accounts, monkeypatch, resource_store):
    client, db, _, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    storage.delete(shared.object_key(access.user_id, item["path"]))
    assert client.get(f"/resources/{item['id']}/download").status_code == 404
    assert client.get("/resources").json()["items"] == []


def test_same_size_edit_invalidates_cache_metadata(accounts, monkeypatch, resource_store):
    client, db, _, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client, body=b"old")
    row, version = workspace_files.resolve_input(db, access, {"id": item["id"]})
    etag = row.live_etag
    storage.objects[shared.object_key(access.user_id, item["path"])] = b"new"
    row, version = workspace_files.resolve_input(db, access, {"id": item["id"]})
    assert row.live_etag != etag and not version.content_hash
    assert client.get(f"/resources/{item['id']}/download").content == b"new"


def test_api_write_replaces_one_current_object(accounts, monkeypatch, resource_store):
    client, db, _, access, storage = mounted(accounts, monkeypatch, resource_store)
    item = upload(client)
    workspace_files._save_bytes(access.user_id, access, db, item["path"], b"API edit", "text/plain")
    assert storage.objects[shared.object_key(access.user_id, item["path"])] == b"API edit"
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1
    assert not any("file-operations/" in key for key in storage.objects)


def legacy_resource(client, db, storage, *, name="legacy.txt"):
    # Build a pre-mount fixture without current-directory discovery marking
    # previously staged legacy keys as externally deleted.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shared, "refresh_index", lambda *args, **kwargs: (None, False))
        item = upload(client, name)
    current = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == UUID(item["id"])))
    original = current.object_key
    client.post(f"/resources/{item['id']}/attachment")
    key = f"legacy/{item['id']}.txt"
    storage.objects[key] = storage.objects.pop(original)
    current.object_key = key
    from app.models import ChatAttachment
    attached = db.scalar(select(ChatAttachment).where(ChatAttachment.resource_file_id == UUID(item["id"])))
    attached.object_key = key
    db.commit()
    storage.copies.clear()
    return item, key, original


def test_migration_preflight_checks_missing_and_conflicting_objects_before_copy(accounts, monkeypatch, resource_store):
    from app.migrate_shared_resources import migrate
    client, db, _, _, storage = mounted(accounts, monkeypatch, resource_store)
    first, old, target = legacy_resource(client, db, storage)
    second, missing, _ = legacy_resource(client, db, storage, name="missing.txt")
    body = storage.objects.pop(missing)
    with pytest.raises(ValueError, match="对象缺失"):
        migrate(db, execute=True, offline=True)
    assert not storage.copies and storage.head(target) is None
    storage.objects[missing] = body
    storage.objects[target] = b"different live content"
    with pytest.raises(ValueError, match="未覆盖"):
        migrate(db)
    assert not storage.copies and storage.objects[target] == b"different live content"


def test_migration_preserves_current_attachment_and_can_be_repeated(accounts, monkeypatch, resource_store):
    from app.migrate_shared_resources import migrate
    client, db, _, _, storage = mounted(accounts, monkeypatch, resource_store)
    item, old, target = legacy_resource(client, db, storage)
    assert migrate(db)["verified"] == 1 and not storage.copies
    with pytest.raises(ValueError, match="停止"):
        migrate(db, execute=True)
    assert migrate(db, execute=True, offline=True)["verified"] == 1
    assert storage.objects[old] == storage.objects[target]
    assert client.get(f"/resources/{item['id']}/download").content == b"resource facts"
    copies = len(storage.copies)
    assert migrate(db, execute=True, offline=True)["already_mounted"] == 1
    assert len(storage.copies) == copies
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1
