import io
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
    def __init__(self): self.objects = {}
    def put(self, key, source, *_): self.objects[key] = source.read()
    def download(self, key): return io.BytesIO(self.objects[key])
    def delete(self, key): self.objects.pop(key, None)
    def iter_objects(self, prefix=None):
        for key, body in list(self.objects.items()):
            if not prefix or key.startswith(prefix): yield {"Key": key, "Size": len(body)}


def configure_mount(db, monkeypatch, storage):
    settings = db.get(CloudComputerSettings, 1)
    settings.config = {**settings.config, "oss_role_arn": ROLE}
    db.commit()
    monkeypatch.setattr(storage_backend, "active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket=storage.bucket, region=storage.region))
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: storage)


def test_mount_snapshot_is_workspace_scoped_and_reuses_metered_cache(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    own = upload(client, "own.txt", b"own data")
    other = accounts[0][2]
    register(other, "other@example.com")
    upload(other, "other.txt", b"private other workspace")
    storage = MemoryMount()
    configure_mount(db, monkeypatch, storage)
    command = submit(db, access, conversation)
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, [])
    prefix = row.snapshot["resource_mount"]["prefix"]
    assert prefix.startswith(f"workspaces/{access.workspace_id}/cloud-computer/{row.id}/")
    assert storage.objects == {prefix + own["path"]: b"own data"}
    assert list(row.manifest) == [own["path"]]
    reads = len(resource_store[1])
    cloud.stage_mount(db, row, access, [])
    assert len(resource_store[1]) == reads == 1
    assert client.get(f"/resources/{own['id']}/download?version=1").content == b"own data"


def test_mount_recovery_saves_new_version_and_cleanup_is_session_only(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    item = upload(client, "resume.txt", b"original")
    storage = MemoryMount()
    configure_mount(db, monkeypatch, storage)
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
    assert storage.objects == {"workspaces/other/cloud-computer/other/workspace/keep.txt": b"keep"}
    assert row.snapshot["mount_cleaned"] is True


def test_mount_conflict_retains_working_copy_and_original_versions(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    item = upload(client, "resume.txt", b"original")
    storage = MemoryMount()
    configure_mount(db, monkeypatch, storage)
    command = submit(db, access, conversation)
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, [])
    prefix = row.snapshot["resource_mount"]["prefix"]
    storage.objects[prefix + item["path"]] = b"cloud edit"
    workspace_files._save_bytes(access.workspace_id, access, db, item["path"], b"external edit", expected_version=1)
    with pytest.raises(HTTPException) as error:
        cloud.recover_mount(db, row)
    assert error.value.status_code == 409
    row.error = "needs recovery"
    cloud.cleanup_mount(db, row)
    assert storage.objects[prefix + item["path"]] == b"cloud edit"
    assert client.get(f"/resources/{item['id']}/download?version=2").content == b"external edit"


def test_mount_explicit_version_and_deleted_resource(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    item = upload(client, "resume.txt", b"version one")
    workspace_files._save_bytes(access.workspace_id, access, db, item["path"], b"version two", expected_version=1)
    storage = MemoryMount()
    configure_mount(db, monkeypatch, storage)
    command = submit(db, access, conversation, inputs=[{"id": item["id"], "version": 1}])
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, command.payload["inputs"])
    prefix = row.snapshot["resource_mount"]["prefix"]
    assert storage.objects[prefix + item["path"]] == b"version one"
    client.delete(f"/resources/{item['id']}")
    cloud.stage_mount(db, row, access, [])
    assert not storage.objects and not row.manifest


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
        assert computer.snapshot.get("mount_cleaned")
        assert not list(storage.iter_objects(computer.snapshot["resource_mount"]["prefix"]))
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.capability == "cloud_computer")))
        assert sum(event.credits for event in events) == computer.charged_credits
        print("live_mount_verified", {"seconds": computer.billed_seconds, "credits": str(computer.charged_credits),
            "versions": [1, 2], "held": str(computer.held_credits), "mount_cleaned": True})
    finally:
        db.rollback()
        if computer:
            provider = cloud.adapter(computer.snapshot, computer.encrypted_api_key)
            for identifier, _ in provider.find(computer.id):
                assert provider.kill(identifier)
            computer.error = None
            cloud.cleanup_mount(db, computer)
