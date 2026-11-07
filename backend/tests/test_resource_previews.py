import hashlib
import io
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import auth, resource_previews as preview, storage_billing
from app.models import ResourcePreview, WorkspaceFile
from app.storage_models import StorageTransfer
from test_auth import register, share, switch
from test_cloud_computer_mount import MemoryMount


@pytest.fixture
def store(monkeypatch):
    storage = MemoryMount()
    monkeypatch.setattr(preview, "current_storage", lambda *_args, **_kwargs: storage)
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: storage)
    monkeypatch.setattr("app.storage_downloads.download_range_fileobj", lambda key, start, end: io.BytesIO(storage.objects[key][start:end+1]))
    monkeypatch.setattr(preview, "dispatch", lambda _: None)
    return storage


def resource(db, client, storage, path="photo.png", data=None):
    identity = register(client)
    if data is None:
        stream = io.BytesIO()
        Image.new("RGB", (800, 600), "#0a9a8f").save(stream, "PNG")
        data = stream.getvalue()
    row = WorkspaceFile(workspace_id=UUID(identity["workspace_id"]), path=path,
        content_type="image/png" if path.endswith(".png") else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data), content_hash=hashlib.sha256(data).hexdigest(), current_version=1,
        source="resource_upload", created_by=UUID(identity["user"]["id"]), updated_by=UUID(identity["user"]["id"]),
        expires_at=auth.now()+timedelta(days=365))
    db.add(row); db.commit()
    storage.objects[preview.object_key(row.workspace_id, path)] = data
    return row, identity


def test_preview_generation_dedup_range_billing_and_no_storage_charge(accounts, store):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store)
    before = storage_billing.inventory(db, row.workspace_id)["used_bytes"]
    path = f"/resources/{row.id}/preview"
    first = client.post(path, json={"kind":"thumbnail"})
    assert first.status_code == 202, first.text
    assert client.post(path, json={"kind":"thumbnail"}).json()["id"] == first.json()["id"]
    assert not list(db.scalars(select(StorageTransfer)))
    preview.run(first.json()["id"], sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    info = client.get(path).json()
    assert info["previews"][0]["status"] == "ready", info
    cover = info["previews"][0]
    assert cover["width"] == 640 and cover["height"] == 480
    assert storage_billing.inventory(db, row.workspace_id)["used_bytes"] == before
    head = client.head(cover["url"])
    assert head.status_code == 200
    assert not list(db.scalars(select(StorageTransfer)))
    response = client.get(cover["url"], headers={"Range":"bytes=0-9"})
    assert response.status_code == 206 and len(response.content) == 10
    db.expire_all()
    assert sum(item.sent_bytes for item in db.scalars(select(StorageTransfer))) == 10


def test_permissions_delete_same_size_update_and_expired_cache(accounts, store):
    (owner, stranger, member), db = accounts
    row, identity = resource(db, owner, store)
    register(stranger, "stranger@example.com"); register(member, "reader@example.com")
    path = f"/resources/{row.id}/preview"
    assert stranger.get(path).status_code == 404
    assert stranger.post(path, json={"kind":"thumbnail"}).status_code == 404
    share(owner, "reader@example.com", "viewer"); switch(member, identity["workspace_id"])
    assert member.get(path).status_code == 200
    identifier = member.post(path, json={"kind":"thumbnail"}).json()["id"]
    preview.run(identifier, sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    old = member.get(path).json()
    original = preview.object_key(row.workspace_id, row.path)
    store.objects[original] = b"x" * len(store.objects[original])
    changed = member.get(path).json()
    assert old["revision"] != changed["revision"] and changed["previews"] == []
    assert member.get(old["previews"][0]["url"]).status_code == 409
    assert member.get(old["original_url"]).status_code == 409
    row.deleted_at=auth.now(); db.commit()
    assert member.get(path).status_code == 404
    preview.maintain(sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    db.expire_all()
    assert db.get(ResourcePreview, UUID(identifier)).status == "expired"
    assert original in store.objects


def test_word_only_on_open_limits_and_recovery(accounts, store, monkeypatch):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store, "report.docx", b"invalid word")
    path = f"/resources/{row.id}/preview"
    assert client.post(path, json={"kind":"thumbnail"}).json()["status"] == "not_prepared"
    assert not list(db.scalars(select(ResourcePreview)))
    first = client.post(path, json={"kind":"document"}).json()
    monkeypatch.setattr(preview, "generate", lambda *_: (_ for _ in ()).throw(RuntimeError("文件已加密，请下载查看")))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    preview.run(first["id"], factory)
    item = client.get(path).json()["previews"][0]
    assert item["status"] == "failed" and "加密" in item["error"]
    retry = client.post(path, json={"kind":"document", "retry":True}).json()
    assert retry["id"] == first["id"] and retry["status"] == "queued"
    db.expire_all(); task = db.get(ResourcePreview, UUID(first["id"]))
    task.status="running"; task.updated_at=auth.now()-timedelta(minutes=6); db.commit()
    delivered=[]; monkeypatch.setattr(preview,"dispatch",delivered.append)
    preview.maintain(factory)
    assert task.id in delivered


def test_generation_discards_modified_source(accounts, store, monkeypatch):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store)
    first = client.post(f"/resources/{row.id}/preview", json={"kind":"thumbnail"}).json()
    original_generate = preview.generate
    def modify(*args):
        result = original_generate(*args)
        key=preview.object_key(row.workspace_id,row.path)
        store.objects[key]=b"x"*len(store.objects[key])
        return result
    monkeypatch.setattr(preview,"generate",modify)
    preview.run(first["id"],sessionmaker(bind=db.get_bind(),expire_on_commit=False))
    db.expire_all()
    assert db.get(ResourcePreview,UUID(first["id"])).status == "failed"
    assert not any(key.startswith(preview.PREFIX) for key in store.objects)


def test_word_cover_upload_failure_reclaims_both_outputs(accounts, store, monkeypatch):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store, "report.docx", b"word")
    first = client.post(f"/resources/{row.id}/preview", json={"kind":"document"}).json()
    def render(_source, folder, *_args):
        output, cover = folder / "document.pdf", folder / "thumbnail.webp"
        output.write_bytes(b"pdf"); cover.write_bytes(b"webp")
        return output, (600, 800), cover
    monkeypatch.setattr(preview, "generate", render)
    put = store.put
    def failing_put(key, body, content_type):
        put(key, body, content_type)
        if key.endswith(".cover"):
            raise OSError("synthetic upload failure")
    monkeypatch.setattr(store, "put", failing_put)
    preview.run(first["id"], sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    db.expire_all()
    assert db.get(ResourcePreview, UUID(first["id"])).status == "failed"
    assert not any(key.startswith(preview.PREFIX) for key in store.objects)


def test_recovery_reclaims_interrupted_upload_and_lru_does_not_touch_original(accounts, store, monkeypatch):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store)
    identifier = client.post(f"/resources/{row.id}/preview", json={"kind":"thumbnail"}).json()["id"]
    item = db.get(ResourcePreview, UUID(identifier))
    item.status, item.object_key = "running", preview.PREFIX+"interrupted"
    item.updated_at = auth.now()-timedelta(minutes=6)
    store.objects[item.object_key] = b"partial"
    store.objects[item.object_key+".cover"] = b"partial-cover"
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    preview.maintain(factory)
    assert not any(key.startswith(preview.PREFIX) for key in store.objects)
    preview.run(identifier, factory)
    db.expire_all(); item = db.get(ResourcePreview, UUID(identifier))
    assert item.status == "ready"
    item.accessed_at = auth.now()-timedelta(days=8); db.commit()
    preview.maintain(factory)
    db.expire_all()
    assert db.get(ResourcePreview, UUID(identifier)).status == "expired"
    assert preview.object_key(row.workspace_id, row.path) in store.objects


def test_missing_cache_object_can_be_regenerated(accounts, store):
    (client, _, _), db = accounts
    row, _ = resource(db, client, store)
    path = f"/resources/{row.id}/preview"
    identifier = client.post(path, json={"kind":"thumbnail"}).json()["id"]
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    preview.run(identifier, factory)
    db.expire_all(); item = db.get(ResourcePreview, UUID(identifier))
    store.delete(item.object_key)
    assert client.get(path).json()["previews"][0]["status"] == "expired"
    assert client.post(path, json={"kind":"thumbnail", "retry":True}).json()["id"] == identifier
    preview.run(identifier, factory)
    assert client.get(path).json()["previews"][0]["status"] == "ready"
