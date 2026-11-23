import io
import hashlib
import os
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

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
    def download_range(self, key, start, end): return io.BytesIO(self.objects[key][start:end + 1])
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
    from app.models import SiteStorageSettings
    row = db.get(SiteStorageSettings, 2)
    if row is None:
        db.add(SiteStorageSettings(id=2, version=1, provider="oss", bucket=storage.bucket,
            region=storage.region, encrypted_secret_id="test-id", encrypted_secret_key="test-secret", active=True))
        db.commit()
    monkeypatch.setattr(storage_backend, "active_settings", lambda _db: SimpleNamespace(
        provider="oss", bucket=storage.bucket, region=storage.region))
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: storage)
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: storage)


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
    assert prefix == f"accounts/{access.user_id}/resource-directory/"
    knowledge = row.snapshot["resource_mount"]["knowledge_prefix"]
    assert knowledge == f"accounts/{access.user_id}/knowledge-originals/"
    assert storage.objects[knowledge + ".rag-directory"] == b""
    assert storage.objects[prefix + own["path"]] == b"own data"
    assert not any(path.startswith(prefix) and path.endswith('other.txt') for path in storage.objects)
    assert row.manifest == {}
    reads = len(resource_store[1])
    copies = len(storage.copies)
    cloud.stage_mount(db, row, access, [])
    assert len(resource_store[1]) == reads == 0
    assert len(storage.copies) == copies
    assert client.get(f"/resources/{own['id']}/download?version=1").content == b"own data"


def test_knowledge_file_tools_use_the_owned_mount_and_hide_other_accounts(accounts, monkeypatch, tmp_path):
    from app.cloud_file_operation import perform
    from app.knowledge_originals import MOUNT_ROOT, prefix
    from app.models import Document
    from test_auth import library

    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    other = accounts[0][2]
    register(other, "other@example.com")
    own_library, other_library = library(client), library(other)
    storage = MemoryMount()
    configure_mount(db, monkeypatch, storage)
    monkeypatch.setattr("app.main.process_document.delay", lambda _: None)

    def upload_original(owner, library_id, content):
        response = owner.post(f"/knowledge-bases/{library_id}/documents",
            files={"file": ("same.txt", content, "text/plain")})
        assert response.status_code == 201, response.text
        return db.get(Document, UUID(response.json()["id"]))

    own = upload_original(client, own_library, b"my text")
    stranger = upload_original(other, other_library, b"other text")
    computer = db.get(cloud.Computer, submit(db, access, conversation).session_id)
    own_path = own.object_key.removeprefix(prefix(access.user_id))
    stranger_path = stranger.object_key.split("/knowledge-originals/", 1)[1]
    mounted = tmp_path / own_path
    mounted.parent.mkdir(parents=True)
    mounted.write_bytes(storage.objects[own.object_key])
    (tmp_path / ".rag-directory").touch()

    def run(db, access, conversation_id, request_id, arguments, *_args, **_kwargs):
        cloud.owned_computer(db, access, arguments["computer_id"])
        return {"status": "completed", "file_result": {
            "value": perform(arguments["file_operation"], knowledge_root=tmp_path)}}

    monkeypatch.setattr(cloud, "execute", run)

    def tool(name, **arguments):
        return workspace_files.execute_tool(name, {"computer_id": str(computer.id), **arguments},
            db, access, access.user_id, conversation.id, uuid4())

    assert tool("list_files", knowledge=True, query="same")["items"] == [{
        "path": f"{MOUNT_ROOT}/{own_path}", "filename": "same.txt",
        "document_id": str(own.id), "size_bytes": len(b"my text"),
    }]
    assert tool("read_file", path=f"{MOUNT_ROOT}/{own_path}")["content"] == "my text"
    with pytest.raises(HTTPException) as error:
        tool("read_file", path=f"{MOUNT_ROOT}/{stranger_path}")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        tool("list_files", knowledge=True, computer_id=str(uuid4()))
    assert error.value.status_code == 404


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
    assert result == []
    assert client.get(f"/resources/{item['id']}/download").content == b"modified"
    assert {entry["path"] for entry in client.get("/resources").json()["items"]} == {item["path"], "new.txt"}
    assert cloud.recover_mount(db, row) == []
    cloud.cleanup_mount(db, row)
    assert storage.objects[prefix + item["path"]] == b"modified"
    assert storage.objects[prefix + "new.txt"] == b"new output"
    assert not row.snapshot.get("mount_cleaned")
    assert client.delete(f"/resources/{item['id']}").status_code == 204
    assert prefix + item["path"] not in storage.objects
    assert storage.objects[prefix + "new.txt"] == b"new output"


def test_mount_direct_edit_and_explicit_overwrite_have_no_history(accounts, monkeypatch, resource_store):
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
    assert client.get(f"/resources/{item['id']}/download").content == b"cloud edit"
    workspace_files._save_bytes(access.user_id, access, db, item["path"], b"external edit")
    cloud.cleanup_mount(db, row)
    assert storage.objects[prefix + item["path"]] == b"external edit"
    assert cloud.recover_mount(db, row) == []
    assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1


def test_mount_never_stages_legacy_input_versions(accounts, monkeypatch, resource_store):
    client, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    item = upload(client, "resume.txt", b"version one")
    workspace_files._save_bytes(access.user_id, access, db, item["path"], b"version two", expected_version=1)
    command = submit(db, access, conversation, inputs=[{"id": item["id"], "version": 1}])
    row = db.get(cloud.Computer, command.session_id)
    cloud.stage_mount(db, row, access, command.payload["inputs"])
    prefix = row.snapshot["resource_mount"]["prefix"]
    assert storage.objects[prefix + item["path"]] == b"version two"
    class HistoricalInput:
        def write(self, *args):
            pytest.fail("mounted resource was staged")
    cloud.stage_mount(db, row, access, command.payload["inputs"], HistoricalInput(), object())
    client.delete(f"/resources/{item['id']}")
    cloud.stage_mount(db, row, access, [])
    assert prefix + item["path"] not in storage.objects and not row.manifest


def test_legacy_mount_mode_cannot_read_resource_bodies(accounts, monkeypatch):
    _, db, conversation, access, _, _ = setup(accounts, monkeypatch)
    from app.models import CloudComputerSession
    from uuid import uuid4
    row = CloudComputerSession(id=uuid4(), account_id=access.user_id, user_id=access.user_id,
        conversation_id=conversation.id, session_hash=access.session_hash, status="ready",
        snapshot={"resource_mount": {"mode": "legacy"}}, encrypted_api_key="test",
        manifest={}, held_credits=0, charged_credits=0, billed_seconds=0, billing_sequence=0)
    db.add(row); db.commit()
    monkeypatch.setattr("app.resource_cache.read_bytes", lambda *_a, **_k: pytest.fail("legacy body read"))
    with pytest.raises(HTTPException) as error:
        cloud.synchronize(db, row, None, None)
    assert error.value.status_code == 503


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
    from app.knowledge_originals import mount_path
    from app.models import Document
    from pdf_fixtures import make_pdf
    from test_auth import library
    monkeypatch.setattr("app.main.process_document.delay", lambda _: None)
    pdf, replacement = make_pdf(), make_pdf(page_count=3)
    kb, other_kb = library(client), library(client)

    def upload_pdf(library_id, body, previous=None):
        response = client.post(f"/knowledge-bases/{library_id}/documents",
            files={"file": ("evidence.pdf", body, "application/pdf")},
            data={"replace_document_id": str(previous["id"]), "expected_content_hash": previous["content_hash"]}
            if previous else {})
        assert response.status_code == 201, response.text
        return response.json()

    first = upload_pdf(kb, pdf)
    second = upload_pdf(other_kb, pdf)
    first_path, second_path = (mount_path(db.get(Document, UUID(doc["id"])), access.user_id)
                               for doc in (first, second))
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
        assert client.get(f"/resources/{item['id']}/download").content == b"synthetic edited"
        assert len(db.scalars(select(WorkspaceFileVersion)).all()) == 1
        reads = len(resource_store[1])
        run("python3 -c \"from pathlib import Path; assert Path('new.txt').read_text()=='persistent output'; print('second command retained files')\"")
        assert len(resource_store[1]) == reads

        def check_original(path, body, page):
            script = (f"from pathlib import Path; import hashlib; p=Path({path!r}); "
                      f"assert hashlib.sha256(p.read_bytes()).hexdigest()=={hashlib.sha256(body).hexdigest()!r}; "
                      "assert p.read_bytes().startswith(b'%PDF-')")
            run("python3 -c " + shlex.quote(script))
            run(f"pdftotext -f {page} -l {page} {shlex.quote(path)} - | "
                f"grep -F {shlex.quote(f'Page {page} evidence')}")

        check_original(first_path, pdf, 1)
        check_original(second_path, pdf, 2)
        read_only_probe = first_path.rsplit("/", 1)[0] + "/write-probe.txt"
        run("test ! -e " + shlex.quote(read_only_probe) + " && ! touch " + shlex.quote(read_only_probe))
        replaced = upload_pdf(kb, replacement, first)
        assert replaced["id"] == first["id"]
        check_original(first_path, replacement, 3)
        assert client.delete(f"/documents/{first['id']}").status_code == 200
        run("test ! -e " + shlex.quote(first_path))
        assert client.post(f"/documents/{first['id']}/restore").status_code == 200
        check_original(first_path, replacement, 3)
        print("knowledge_mount_verified", {"computer_id": str(computer.id),
            "originals": [{"path": first_path, "page": 1, "sha256": hashlib.sha256(pdf).hexdigest()},
                          {"path": second_path, "page": 2, "sha256": hashlib.sha256(pdf).hexdigest()},
                          {"path": first_path, "page": 3, "sha256": hashlib.sha256(replacement).hexdigest()}]})
        assert client.post(f"/cloud-computers/{computer.id}/end", json={}).status_code == 200
        cloud.maintenance(factory)
        db.expire_all()
        computer = db.get(cloud.Computer, computer.id)
        assert computer.ended_at and computer.held_credits == 0
        assert computer.snapshot["resource_mount"]["mode"] == "shared"
        assert list(storage.iter_objects(computer.snapshot["resource_mount"]["prefix"]))
        events = list(db.scalars(select(UsageEvent).where(UsageEvent.capability == "cloud_computer")))
        assert sum(event.credits for event in events) == computer.charged_credits
        print("live_mount_verified", {"seconds": computer.billed_seconds, "credits": str(computer.charged_credits),
            "current_file_only": True, "held": str(computer.held_credits), "shared_directory_retained": True})
    finally:
        db.rollback()
        if computer:
            provider = cloud.adapter(computer.snapshot, computer.encrypted_api_key)
            for identifier, _ in provider.find(computer.id):
                assert provider.kill(identifier)
            computer.error = None
            cloud.cleanup_mount(db, computer)
            # Test-only explicit cleanup, never part of instance destruction.
            for key in list(storage.iter_objects(f"accounts/{access.user_id}/")):
                storage.delete(key["Key"])
