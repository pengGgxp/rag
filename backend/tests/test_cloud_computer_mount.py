import io
import hashlib
import os
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import cloud_computer as cloud, storage_backend, workspace_files
from app.models import CloudComputerSettings, WorkspaceFile, WorkspaceFileVersion, UsageEvent
from test_auth import register
from test_cloud_computer import setup, submit
from test_resources import resource_store, upload

ROLE = "acs:ram::1326464773986036:role/sandbox"


class MemoryMount:
    bucket, region = "test-bucket", "cn-beijing"
    def __init__(self): self.objects, self.copies = {}, []
    def put(self, key, source, *_): self.objects[key] = source.read()
    def download(self, key): return io.BytesIO(self.objects[key])
    def delete(self, key): self.objects.pop(key, None)
    def head(self, key):
        if key not in self.objects: return None
        return {"Content-Length": len(self.objects[key]), "ETag": hashlib.md5(self.objects[key]).hexdigest()}
    def copy(self, source, target, *, etag=None):
        assert not etag or self.head(source)["ETag"] == etag
        self.copies.append((source, target))
        self.objects[target] = self.objects[source]
    def iter_objects(self, prefix=None):
        for key, body in list(self.objects.items()):
            if not prefix or key.startswith(prefix): yield {"Key": key, "Size": len(body), "ETag": self.head(key)["ETag"]}


def configure_mount(db, monkeypatch, storage):
    settings = db.get(CloudComputerSettings, 1)
    settings.config = {**settings.config, "oss_role_arn": ROLE}
    db.commit()
    monkeypatch.setattr(storage_backend, "active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket=storage.bucket, region=storage.region))
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: storage)


def test_mount_is_persistent_workspace_directory_without_cache_or_staging(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    own = upload(client, "own.txt", b"own data")
    other = accounts[0][2]
    register(other, "other@example.com")
    upload(other, "other.txt", b"private other workspace")
    command = submit(db, access, conversation)
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, [])
    prefix = row.snapshot["resource_mount"]["prefix"]
    assert prefix == f"workspaces/{access.workspace_id}/resource-directory/"
    assert storage.objects[prefix + own["path"]] == b"own data"
    assert not any(path.startswith(prefix) and path.endswith('other.txt') for path in storage.objects)
    assert list(row.manifest) == [own["path"]]
    reads = len(resource_store[1])
    copies = len(storage.copies)
    cloud.stage_mount(db, row, access, [])
    assert len(resource_store[1]) == reads == 0
    assert len(storage.copies) == copies
    assert client.get(f"/resources/{own['id']}/download?version=1").content == b"own data"


def test_mount_recovery_indexes_changes_and_never_cleans_shared_files(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    item = upload(client, "resume.txt", b"original")
    command = submit(db, access, conversation)
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, [])
    prefix = row.snapshot["resource_mount"]["prefix"]
    storage.objects[prefix + item["path"]] = b"modified"
    storage.objects[prefix + "new.txt"] = b"new output"
    storage.objects["workspaces/other/cloud-computer/other/workspace/keep.txt"] = b"keep"
    result = cloud.recover_mount(db, row)
    assert {entry["version"] for entry in result} == {1, 2}
    assert client.get(f"/resources/{item['id']}/download?version=1").content == b"original"
    assert client.get(f"/resources/{item['id']}/download?version=2").content == b"modified"
    assert cloud.recover_mount(db, row) == []
    cloud.cleanup_mount(db, row)
    assert storage.objects[prefix + item["path"]] == b"modified"
    assert storage.objects[prefix + "new.txt"] == b"new output"
    assert not row.snapshot.get("mount_cleaned")
    assert client.delete(f"/resources/{item['id']}").status_code == 204
    assert prefix + item["path"] not in storage.objects
    assert storage.objects[prefix + "new.txt"] == b"new output"


def test_mount_unsaved_changes_block_stale_api_overwrite(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    item = upload(client, "resume.txt", b"original")
    command = submit(db, access, conversation)
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, [])
    prefix = row.snapshot["resource_mount"]["prefix"]
    storage.objects[prefix + item["path"]] = b"cloud edit"
    with pytest.raises(HTTPException) as error:
        workspace_files._save_bytes(access.workspace_id, access, db, item["path"], b"external edit", expected_version=1)
    assert error.value.status_code == 409
    row.error = "needs recovery"
    cloud.cleanup_mount(db, row)
    assert storage.objects[prefix + item["path"]] == b"cloud edit"
    assert client.get(f"/resources/{item['id']}/download?version=1").content == b"original"
    assert cloud.recover_mount(db, row)[0]["version"] == 2
    assert client.get(f"/resources/{item['id']}/download?version=2").content == b"cloud edit"


def test_mount_historical_input_does_not_replace_shared_current_file(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    item = upload(client, "resume.txt", b"version one")
    workspace_files._save_bytes(access.workspace_id, access, db, item["path"], b"version two", expected_version=1)
    command = submit(db, access, conversation, inputs=[{"id": item["id"], "version": 1}])
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, command.payload["inputs"])
    prefix = row.snapshot["resource_mount"]["prefix"]
    assert storage.objects[prefix + item["path"]] == b"version two"
    class HistoricalInput:
        def write(self, sandbox, path, data, root):
            assert path == f"{item['id']}/1/{item['path']}"
            assert data == b"version one"
            assert root == "/tmp/zhiku-cloud-computer/inputs"
    cloud.stage_mount(db, row, access, command.payload["inputs"], HistoricalInput(), object())
    client.delete(f"/resources/{item['id']}")
    cloud.stage_mount(db, row, access, [])
    assert prefix + item["path"] not in storage.objects and not row.manifest


@pytest.mark.skipif(os.environ.get("RAG_MOUNT_LIVE_ACCEPT") != "1", reason="explicit paid cloud mount opt-in required")
def test_live_mounted_resource_versions_billing_and_destroy(accounts, monkeypatch, resource_store):
    from app import oss_storage
    from app.cloud_computer_adapter import AlibabaSandbox
    from app.site_auth import protect, reveal
    client, db, conversation, access, _, factory = setup(accounts, monkeypatch, rate="1")
    raw = Path(os.environ["RAG_MOUNT_CREDENTIAL_FILE"]).read_text(encoding="utf-8-sig")
    cloud_key = re.search(r"(?im)^\s*apikey\s*[:：=]\s*(\S+)", raw).group(1)
    access_id = re.search(r"LTAI[A-Za-z0-9]+", raw).group(0)
    secret_line = next(line for line in raw.splitlines() if re.search(r"(?i)access\s*key\s*secret", line))
    secret = re.sub(r"(?i)^.*?access\s*key\s*secret\s*[:：=]?\s*", "", secret_line).strip()
    storage = oss_storage.OssStorage(SimpleNamespace(bucket="zhikubiao", region="cn-beijing",
        encrypted_secret_id=protect(access_id), encrypted_secret_key=protect(secret)))
    configure_mount(db, monkeypatch, storage)
    # All production paths point at the same actual store for this opt-in probe.
    monkeypatch.setattr("app.chat_attachments.upload_fileobj", lambda stream, key, mime: storage.put(key, stream, mime))
    monkeypatch.setattr("app.workspace_files.upload_fileobj", lambda stream, key, mime: storage.put(key, stream, mime))
    monkeypatch.setattr("app.storage_downloads.download_range_fileobj", storage.download_range)
    monkeypatch.setattr("app.storage.delete_object", storage.delete)
    settings = db.get(CloudComputerSettings, 1)
    settings.config = {**settings.config, "template": "eidpiyb7xazavqt0n4go", "max_seconds": 180, "idle_seconds": 60}
    settings.encrypted_api_key = protect(cloud_key)
    db.commit()
    monkeypatch.setattr(cloud, "adapter", lambda config, encrypted: AlibabaSandbox(config, reveal(encrypted)))
    item = upload(client, "resume.txt", b"synthetic original")
    computer = None
    try:
        def run(command):
            nonlocal computer
            entry = submit(db, access, conversation, command)
            computer = db.get(cloud.Computer, entry.session_id)
            cloud.run(entry.id, factory)
            db.expire_all()
            entry = db.get(cloud.Command, entry.id)
            assert entry.status == "completed", entry.result
            return entry
        import shlex
        script = "from pathlib import Path; p=Path(" + repr(item["path"]) + "); assert p.read_bytes()==b'synthetic original'; p.write_text('synthetic edited'); Path('new.txt').write_text('persistent output'); print('mounted edit complete')"
        run("python3 -c " + shlex.quote(script))
        assert client.get(f"/resources/{item['id']}/download?version=1").content == b"synthetic original"
        assert client.get(f"/resources/{item['id']}/download?version=2").content == b"synthetic edited"
        reads = len(resource_store[1])
        run("python3 -c \"from pathlib import Path; assert Path('new.txt').read_text()=='persistent output'; print('second command retained files')\"")
        assert len(resource_store[1]) == reads
        assert client.post(f"/conversations/{conversation.id}/cloud-computer/end", json={}).status_code == 200
        cloud.maintenance(factory)
        db.expire_all()
        computer = db.get(cloud.Computer, computer.id)
        assert computer.ended_at and computer.held_credits == 0
        assert computer.snapshot["resource_mount"]["mode"] == "shared"
        assert list(storage.iter_objects(computer.snapshot["resource_mount"]["prefix"]))
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.capability == "cloud_computer")))
        assert sum(event.credits for event in events) == computer.charged_credits
        print("live_mount_verified", {"seconds": computer.billed_seconds, "credits": str(computer.charged_credits),
            "versions": [1, 2], "held": str(computer.held_credits), "shared_directory_retained": True})
    finally:
        db.rollback()
        if computer:
            provider = cloud.adapter(computer.snapshot, computer.encrypted_api_key)
            for identifier, _ in provider.find(computer.id):
                assert provider.kill(identifier)
            computer.error = None
            cloud.cleanup_mount(db, computer)
            # Test-only explicit cleanup, never part of instance destruction.
            for key in list(storage.iter_objects(f"workspaces/{access.workspace_id}/")):
                storage.delete(key["Key"])
