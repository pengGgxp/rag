import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, chat_attachments, resource_cache, resources, storage_billing, tasks, workspace_files
from app.config import get_settings
from app.models import ChatAttachment, ChatMessage, Conversation, Document, KnowledgeBase, WorkspaceFile, WorkspaceFileVersion
from app.storage_models import StorageQuotaSettings, StorageTransfer, WorkspaceStorageState
from test_auth import register, share, switch
from test_chat_attachments import PNG


@pytest.fixture
def resource_store(monkeypatch, tmp_path):
    objects, reads = {}, []
    def put(stream, key, *_): objects[key] = stream.read()
    def get(key, start, end):
        reads.append((key, start, end))
        return io.BytesIO(objects[key][start:end+1])
    monkeypatch.setattr(chat_attachments, "upload_fileobj", put)
    monkeypatch.setattr(workspace_files, "upload_fileobj", put)
    monkeypatch.setattr(resource_cache, "download_range_fileobj", get)
    monkeypatch.setattr("app.storage_downloads.download_range_fileobj", get)
    monkeypatch.setattr("app.storage.delete_object", lambda key: objects.pop(key, None))
    monkeypatch.setattr(get_settings(), "resource_cache_dir", str(tmp_path / "cache"))
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    store.objects = objects
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: store)
    return objects, reads


def upload(client, name="facts.txt", body=b"resource facts"):
    result = client.post("/resources", files={"file": (name, body)})
    assert result.status_code == 201, result.text
    return result.json()


def access_for(client, db):
    session = db.get(auth.LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
    return auth.Access(session.user_id, session.workspace_id, "admin", session.token_hash)


def test_upload_shared_reuse_and_conversation_deletion(accounts, resource_store):
    (owner, member, _), db = accounts
    identity = register(owner); register(member, "member@example.com")
    item = upload(owner)
    assert member.get(f"/resources/{item['id']}").status_code == 404
    share(owner, "member@example.com", "viewer"); switch(member, identity["workspace_id"])
    assert member.get("/resources").json()["items"][0]["id"] == item["id"]
    assert member.get(f"/resources/{item['id']}/download").content == b"resource facts"
    assert member.delete(f"/resources/{item['id']}").status_code == 403
    assert member.post("/resources", files={"file": ("x.txt", b"x")}).status_code == 403
    attached = member.post(f"/resources/{item['id']}/attachment?version=1").json()
    row = db.get(ChatAttachment, UUID(attached["id"]))
    conversations = [Conversation(owner_id=row.user_id, workspace_id=row.workspace_id) for _ in range(2)]
    db.add_all(conversations); db.flush()
    messages = [ChatMessage(conversation_id=conversation.id, sequence=0, role="user", content="use",
        attachments=[row]) for conversation in conversations]
    db.add_all(messages); db.commit()
    assert all(message.attachments[0].id == row.id for message in messages)
    for conversation in conversations: conversation.deleted_at = auth.now()
    db.commit()
    chat_attachments.cleanup(db, SimpleNamespace(delete=lambda _: pytest.fail("valid resource deleted")))
    assert owner.get(f"/resources/{item['id']}/download").content == b"resource facts"
    assert owner.delete(f"/resources/{item['id']}").status_code == 204
    assert member.post(f"/resources/{item['id']}/attachment?version=1").status_code == 404
    db.expire_all()
    assert chat_attachments.attachment_read(messages[0].attachments[0])["available"] is False


def test_current_file_and_object_inventory_dedup(accounts, resource_store):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner)
    actor = access_for(owner, db)
    original_usage = storage_billing.inventory(db, actor.workspace_id)["used_bytes"]
    assert original_usage == len(b"resource facts")
    result = workspace_files._save_bytes(actor.workspace_id, actor, db, item["path"], b"changed",
        "text/plain", expected_version=1)
    assert result["version"] == 1
    assert owner.get(f"/resources/{item['id']}/download").content == b"changed"
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1
    assert storage_billing.inventory(db, actor.workspace_id)["used_bytes"] == len(b"changed")


def test_command_uses_mount_without_staging_or_internal_download(accounts, resource_store, monkeypatch):
    (owner, _, _), db = accounts
    register(owner); image = upload(owner, "image.png", PNG); upload(owner, "unused.txt")
    actor = access_for(owner, db)
    from test_cloud_computer import enable_execution
    fake, execute = enable_execution(db, actor, monkeypatch)
    from test_cloud_computer_mount import MemoryMount, configure_mount
    storage = MemoryMount(); storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    execute({"command": "true"})
    assert fake.writes == 0 and not resource_store[1]
    execute({"command": "true"})
    assert fake.writes == 0 and not resource_store[1]
    assert not db.scalars(select(StorageTransfer)).all()
    assert owner.get("/storage/allowances").json()["download_usage"]["model_read"] == 0
    owner.get(f"/resources/{image['id']}/download")
    assert owner.get("/storage/allowances").json()["download_usage"]["user_download"] == len(PNG)


def test_embed_image_in_mounted_document_without_archive_or_binary_stdout(accounts, resource_store, monkeypatch, tmp_path):
    import subprocess
    import sys
    (owner, _, _), db = accounts
    register(owner)
    image = upload(owner, "photo.png", PNG)
    unused = upload(owner, "unused.txt", b"not selected")
    actor = access_for(owner, db)
    document = workspace_files._save_bytes(actor.workspace_id, actor, db, "resume.html",
        b'<html><img src="PHOTO"></html>', "text/html")
    from test_cloud_computer import enable_execution
    fake, execute = enable_execution(db, actor, monkeypatch)
    from test_cloud_computer_mount import MemoryMount, configure_mount
    from app.shared_resources import prefix
    storage = MemoryMount(); storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    def run(sandbox, identifier, command):
        assert not sandbox["files"]
        for key, data in storage.objects.items():
            path = key.removeprefix(prefix(actor.workspace_id))
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        script = ("from pathlib import Path; import base64; from PIL import Image; "
            f"Image.open({image['path']!r}).verify(); p=Path({document['path']!r}); "
            f"data=base64.b64encode(Path({image['path']!r}).read_bytes()).decode(); "
            "p.write_text(p.read_text().replace('PHOTO','data:image/png;base64,'+data)); print('saved')")
        completed = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True)
        assert completed.stdout == "saved\n"
        storage.objects[prefix(actor.workspace_id) + document["path"]] = (tmp_path / document["path"]).read_bytes()
        fake.result = {"exit_code": 0, "stdout": completed.stdout, "stderr": ""}
        return 42
    monkeypatch.setattr(fake, "start", run)
    result = execute({"command": "python embed.py"})
    assert result["status"] == "completed" and not result.get("generated_files")
    assert fake.writes == 0 and not resource_store[1]
    html = owner.get(f"/resources/{document['id']}/download").content
    assert b"data:image/png;base64," + base64.b64encode(PNG) in html
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 3
    assert db.get(WorkspaceFile, UUID(unused["id"])).current_version == 1


def test_range_cache_tamper_and_failed_read_settles_actual_bytes(accounts, resource_store, monkeypatch):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner, body=b"0123456789")
    actor = access_for(owner, db)
    row, version = workspace_files.resolve_input(db, actor, {"id": item["id"], "version": 1})
    assert resource_cache.read_bytes(db, actor, row, version, offset=2, limit=3) == b"234"
    assert resource_cache.read_bytes(db, actor, row, version, offset=2, limit=3) == b"234"
    assert len(resource_store[1]) == 1
    target = next(__import__("pathlib").Path(get_settings().resource_cache_dir).glob("*.bin"))
    target.write_bytes(b"bad")
    assert resource_cache.read_bytes(db, actor, row, version, offset=2, limit=3) == b"234"
    class Interrupted(io.BytesIO):
        def read(self, count=-1):
            if self.tell(): raise IOError("interrupted")
            return super().read(min(count, 2))
    monkeypatch.setattr(resource_cache, "download_range_fileobj", lambda *_: Interrupted(b"0123456789"))
    with pytest.raises(IOError): resource_cache.read_bytes(db, actor, row, version)
    failed = db.scalar(select(StorageTransfer).where(StorageTransfer.status == "failed"))
    assert failed.sent_bytes == 2 and sum(part["reserved"] for part in failed.allocations) == 8
    # Finishing the same transfer again is idempotent and cannot consume more allowance.
    storage_billing.transfer_checkpoint(db, failed.id, finish="failed")
    assert storage_billing.workspace_read(db, actor.workspace_id)["download_usage"]["model_read"] == 8


def test_concurrent_same_version_merges_object_reads(accounts, resource_store):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner)
    actor = access_for(owner, db)
    db.commit()
    def read(_):
        with Session(db.bind) as transaction:
            row, version = workspace_files.resolve_input(transaction, actor, {"id": item["id"], "version": 1})
            return resource_cache.read_bytes(transaction, actor, row, version)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, range(2))) == [b"resource facts", b"resource facts"]
    assert len(resource_store[1]) == 1
    db.expire_all()
    assert len(db.scalars(select(StorageTransfer)).all()) == 1


def test_over_quota_grace_oldest_first_and_document_reference_protected(accounts, resource_store):
    (owner, _, _), db = accounts
    identity = register(owner)
    first = upload(owner, "old.txt", b"old-data")
    second = upload(owner, "new.txt", b"new-data")
    db.get(WorkspaceFile, UUID(first["id"])).created_at = auth.now()-timedelta(days=2)
    db.get(WorkspaceFile, UUID(second["id"])).created_at = auth.now()-timedelta(days=1)
    db.commit()
    actor = access_for(owner, db)
    config = StorageQuotaSettings(id=1, free_storage_bytes=8, free_download_bytes=1000,
        output_limit_bytes=1, retention_days=1, packages=[], costs={})
    db.add(config); db.commit()
    state = storage_billing.workspace_read(db, actor.workspace_id)
    row = db.get(WorkspaceStorageState, actor.workspace_id)
    row.over_quota_since = auth.now()-timedelta(days=29); db.commit()
    storage_billing.maintenance(db)
    assert len(owner.get("/resources").json()["items"]) == 2
    row.over_quota_since = auth.now()-timedelta(days=31); db.commit()
    storage_billing.maintenance(db)
    assert [item["id"] for item in owner.get("/resources").json()["items"]] == [second["id"]]
    assert storage_billing.inventory(db, actor.workspace_id)["used_bytes"] == 8
    # Replenishing capacity clears the continuous-overage clock.
    assert storage_billing.workspace_read(db, actor.workspace_id)["over_quota_since"] is None
    library = KnowledgeBase(workspace_id=actor.workspace_id, name="Protected")
    db.add(library); db.flush()
    version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == UUID(second["id"])))
    kb_key = f"{library.id}/protected.txt"
    resource_store[0][kb_key] = b"kb-data!"
    document = Document(knowledge_base_id=library.id, filename="protected.txt", content_type="text/plain",
        size_bytes=8, object_key=kb_key, content_hash=hashlib.sha256(b"kb-data!").hexdigest())
    db.add(document); db.commit()
    assert owner.delete(f"/resources/{second['id']}").status_code == 204
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tasks, "SessionLocal", __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(bind=db.bind))
    try: tasks.cleanup_workspace_files()
    finally: monkey.undo()
    assert version.object_key not in resource_store[0]
    assert resource_store[0][kb_key] == b"kb-data!"
    assert owner.get("/resources").json()["items"] == []
    assert storage_billing.inventory(db, actor.workspace_id)["used_bytes"] == 8


def test_missing_migrated_object_is_not_reported_as_available(accounts, resource_store):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner)
    version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == UUID(item["id"])))
    resource_store[0].pop(version.object_key)
    assert owner.get("/resources").json()["items"] == []
    assert owner.get(f"/resources/{item['id']}/download").status_code == 404
    assert owner.get(f"/chat/attachments/{item['id']}/content").status_code == 404


def test_cached_reads_revalidate_permissions_and_deleted_paths_stay_deleted(accounts, resource_store):
    (owner, member, _), db = accounts
    identity = register(owner); register(member, "cache-member@example.com")
    item = upload(owner)
    actor = access_for(owner, db)
    row, version = workspace_files.resolve_input(db, actor, {"id": item["id"], "version": 1})
    assert resource_cache.read_bytes(db, actor, row, version) == b"resource facts"
    share(owner, "cache-member@example.com", "viewer"); switch(member, identity["workspace_id"])
    member_actor = access_for(member, db)
    from app.models import Membership
    db.delete(db.scalar(select(Membership).where(Membership.workspace_id == actor.workspace_id,
        Membership.user_id == member_actor.user_id)))
    db.commit()
    with pytest.raises(HTTPException):
        resource_cache.read_bytes(db, member_actor, row, version)
    assert owner.delete(f"/resources/{item['id']}").status_code == 204
    with pytest.raises(HTTPException) as conflict:
        workspace_files._save_bytes(actor.workspace_id, actor, db, item["path"], b"new")
    assert conflict.value.status_code == 409
    assert owner.get(f"/resources/{item['id']}").status_code == 404


def test_patch_propagates_cloud_conflict_without_cached_read(accounts, resource_store, monkeypatch):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner, body=b"old\n")
    actor = access_for(owner, db)
    def forbidden(*args, **kwargs): pytest.fail("file tool read server cache")
    monkeypatch.setattr(resource_cache, "read_bytes", forbidden)
    def concurrent_edit(*args, **kwargs):
        operation = args[4]["file_operation"]
        assert operation["patch"] == "@@ -1 +1 @@\n-old\n+patched\n"
        assert "expected_hash" not in operation
        return {"status": "completed", "file_result": {"error": "文件已变化，请重新读取", "status_code": 409}}
    monkeypatch.setattr("app.cloud_computer.execute", concurrent_edit)
    with pytest.raises(HTTPException) as error:
        workspace_files.execute_tool("apply_patch", {"path": item["path"], "expected_version": 1,
            "patch": "@@ -1 +1 @@\n-old\n+patched\n"}, db, actor, actor.workspace_id)
    assert error.value.status_code == 409
    assert owner.get(f"/resources/{item['id']}/download?version=1").content == b"old\n"


def test_quota_blocks_new_fetch_but_not_cached_version(accounts, resource_store):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner)
    actor = access_for(owner, db)
    row, version = workspace_files.resolve_input(db, actor, {"id": item["id"]})
    resource_cache.read_bytes(db, actor, row, version)
    config = StorageQuotaSettings(id=1, free_storage_bytes=1000, free_download_bytes=0,
        output_limit_bytes=1, retention_days=1, packages=[], costs={})
    db.add(config); db.commit()
    assert resource_cache.read_bytes(db, actor, row, version) == b"resource facts"
    for entry in __import__("pathlib").Path(get_settings().resource_cache_dir).glob("*.bin"):
        entry.unlink()
    with pytest.raises(HTTPException) as error:
        resource_cache.read_bytes(db, actor, row, version)
    assert error.value.status_code == 402
    assert len(resource_store[1]) == 1


def test_image_provider_preparation_uses_resource_cache(accounts, resource_store):
    (owner, _, _), db = accounts
    register(owner); item = upload(owner, "input.png", PNG)
    actor = access_for(owner, db)
    attached = owner.post(f"/resources/{item['id']}/attachment").json()
    row = db.get(ChatAttachment, UUID(attached["id"]))
    messages = [{"role": "user", "content": chat_attachments.user_content("image", [row])}]
    from sqlalchemy.orm import sessionmaker
    context = (sessionmaker(bind=db.bind), actor)
    for _ in range(2):
        result = chat_attachments.model_messages(messages, SimpleNamespace(supports_images=True), context)
        assert result[0]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert len(resource_store[1]) == 1
    assert owner.get("/storage/allowances").json()["download_usage"]["model_read"] == len(PNG)


def test_cache_lru_idle_and_write_failure_does_not_replay_transfer(accounts, resource_store, monkeypatch):
    from pathlib import Path
    import os
    import time
    root = Path(get_settings().resource_cache_dir)
    root.mkdir()
    monkeypatch.setattr(get_settings(), "resource_cache_max_bytes", 6)
    resource_cache.store_bytes(root, "old", b"abc")
    os.utime(root / "old.bin", (time.time()-10, time.time()-10))
    resource_cache.store_bytes(root, "new", b"1234")
    assert not (root / "old.bin").exists() and (root / "new.bin").exists()
    monkeypatch.setattr(get_settings(), "resource_cache_idle_seconds", 1)
    os.utime(root / "new.bin", (time.time()-10, time.time()-10))
    resource_cache.prune_cache()
    assert not list(root.glob("*.bin"))
    (owner, _, _), db = accounts
    register(owner); item = upload(owner)
    actor = access_for(owner, db)
    row, version = workspace_files.resolve_input(db, actor, {"id": item["id"]})
    def unavailable(*_): raise OSError("cache unavailable")
    monkeypatch.setattr(resource_cache, "store_bytes", unavailable)
    assert resource_cache.read_bytes(db, actor, row, version) == b"resource facts"
    assert len(resource_store[1]) == 1
    assert db.scalar(select(StorageTransfer)).status == "completed"
