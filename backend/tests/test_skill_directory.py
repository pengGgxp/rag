"""Managed publication, authorization and hidden resource boundary regressions."""
import base64
import json
import os
import subprocess
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app import cloud_computer as cloud, skill_directory as directory, workspace_files
from app.cloud_file_operation import perform
from app.models import AgentSkill, WorkspaceFile
from test_auth import register
from test_cloud_computer import setup, submit
from test_cloud_computer_mount import configure_mount, MemoryMount
from test_extensions import create
from test_resources import resource_store, upload
from test_skill_script_execution import scripted


def prepared(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    store = MemoryMount()
    configure_mount(db, monkeypatch, store)
    return client, db, conversation, access, fake, factory, store


@pytest.mark.parametrize("path", ["$id", "${id}", "$(printf method)", "*", "?", "[a-f]*",
    "{first,second}", ".", "..", "scripts", "not-a-skill", str(uuid4()) + "*", str(uuid4()) + ".bak"])
def test_shell_expansions_and_non_id_segments_are_not_authorization_ids(path):
    assert directory.required_ids({"command": f'cat "skill-packages/{path}/SKILL.md"'}) == set()


def test_literal_uuid_paths_and_explicit_ids_still_require_authorization():
    first, second = str(uuid4()), str(uuid4())
    assert directory.required_ids({"command": f'cat /home/user/workspace/skill-packages/{first.upper()}/SKILL.md',
        "skill_ids": [second]}) == {first, second}
    assert directory.required_ids({"command": "read_file", "file_operation": {
        "path": f"skill-packages/{first}/SKILL.md"}}) == {first}
    assert directory.required_ids({"command": "start_preview", "preview": {
        "command": f"python skill-packages/{second}/server.py"}}) == {second}


def test_default_publication_includes_platform_and_only_own_private_packages(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, store = prepared(accounts, monkeypatch)
    platform, personal = create(client), scripted(client)
    scripted(client, name="Disabled", enabled=False)
    member, outsider = accounts[0][1:]
    register(member, "member@example.com")
    foreign = scripted(member, name="Same workspace private")
    db.get(AgentSkill, UUID(foreign["id"])).workspace_id = access.workspace_id
    db.commit()
    register(outsider, "outsider@example.com")
    outside = scripted(outsider, name="Other workspace")
    command = submit(db, access, conversation)
    assert set(command.payload["skills"]) == {platform["id"], personal["id"]}
    computer = db.get(cloud.Computer, command.session_id)
    root = directory.prefix(access.workspace_id, access.user_id, computer.id)
    cloud.run(command.id, factory)
    db.refresh(command)
    assert command.status == "completed" and fake.creates == fake.starts == 1
    assert store.objects[root + personal["id"] + "/assets/value.bin"] == b"\xff\x00"
    assert store.objects[root + platform["id"] + "/SKILL.md"] == platform["markdown"].encode()
    assert not any(foreign["id"] in key or outside["id"] in key for key in store.objects)
    assert not db.query(WorkspaceFile).count()
    assert root != directory.prefix(access.workspace_id, uuid4(), computer.id)
    assert root != directory.prefix(uuid4(), access.user_id, computer.id)
    assert root != directory.prefix(access.workspace_id, access.user_id, uuid4())
    assert "resource-directory/" not in root
    assert root == f"workspaces/{access.workspace_id}/skills/{access.user_id.hex}/{computer.id.hex}/"
    assert not root.startswith(computer.snapshot["resource_mount"]["prefix"])
    assert len("/" + root.rstrip("/")) <= 128


def test_request_bytes_fixed_new_request_updates_and_removes_stale_files(accounts, monkeypatch):
    client, db, conversation, access, _, _, store = prepared(accounts, monkeypatch)
    skill = scripted(client)
    packages, versions = directory.current_packages(db, access)
    command = submit(db, access, conversation)
    computer = db.get(cloud.Computer, command.session_id)
    root = directory.prefix(access.workspace_id, access.user_id, computer.id)
    directory.synchronize(db, computer, access, packages, versions)
    response = client.put(f"/agent/skills/{skill['id']}", json={
        "version": skill["version"], "name": skill["name"], "description": skill["description"],
        "markdown": "Updated [dynamic reference](future/file.txt)", "enabled": True,
        "limitations_acknowledged": True, "files": [{"path": "scripts/task.py",
            "content": base64.b64encode(b"print('NEW VERSION')").decode()}]})
    assert response.status_code == 200, response.text
    assert client.put(f"/agent/skills/{skill['id']}", json={
        "version": skill["version"], "name": "stale", "description": "stale", "markdown": "stale"}).status_code == 409
    directory.synchronize(db, computer, access, packages, versions)
    assert store.objects[root + skill["id"] + "/scripts/task.py"] == b"print('PINNED SCRIPT')"
    newer, newer_versions = directory.current_packages(db, access)
    directory.synchronize(db, computer, access, newer, newer_versions)
    assert store.objects[root + skill["id"] + "/scripts/task.py"] == b"print('NEW VERSION')"
    assert root + skill["id"] + "/assets/value.bin" not in store.objects
    assert computer.manifest["skill_versions"] == {skill["id"]: 2}
    assert json.loads(store.objects[root + ".directory.json"])["versions"] == {skill["id"]: 2}
    before = dict(store.objects)
    monkeypatch.setattr(store, "put", lambda *_: pytest.fail("unchanged publication uploaded twice"))
    directory.synchronize(db, computer, access, newer, newer_versions)
    assert store.objects == before


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("revocation", ["disabled", "owner", "workspace"])
def test_worker_rechecks_queued_private_authorization(accounts, monkeypatch, required, revocation):
    client, db, conversation, access, fake, factory, store = prepared(accounts, monkeypatch)
    skill = scripted(client)
    command = submit(db, access, conversation,
        command=f"python skill-packages/{skill['id']}/scripts/task.py" if required else "echo unrelated")
    row = db.get(AgentSkill, UUID(skill["id"]))
    if revocation == "disabled":
        row.enabled = False
    else:
        identity = register(accounts[0][1], "replacement@example.com")
        if revocation == "owner": row.owner_id = UUID(identity["user"]["id"])
        else: row.workspace_id = UUID(identity["workspace_id"])
    db.commit()
    cloud.run(command.id, factory)
    db.refresh(command)
    assert command.status == ("failed" if required else "completed")
    assert fake.creates == fake.starts == (0 if required else 1)
    assert not any(skill["id"] in key for key in store.objects)
    if required: assert "当前不可用" in command.result["error"]


def test_partial_publication_invalidates_old_digest_and_retry_restores_bytes(accounts, monkeypatch):
    client, db, conversation, access, _, _, store = prepared(accounts, monkeypatch)
    skill = scripted(client)
    command = submit(db, access, conversation)
    computer = db.get(cloud.Computer, command.session_id)
    packages, versions = directory.current_packages(db, access)
    directory.synchronize(db, computer, access, packages, versions)
    original = dict(store.objects)
    changed = {skill["id"]: {"SKILL.md": base64.b64encode(b"partial new content").decode()}}
    actual_put = store.put
    def fail_manifest(key, source, *args):
        if key.endswith(".directory.json"): raise OSError("synthetic storage failure")
        return actual_put(key, source, *args)
    monkeypatch.setattr(store, "put", fail_manifest)
    with pytest.raises(OSError):
        directory.synchronize(db, computer, access, changed, {skill["id"]: 2})
    db.expire_all()
    assert "skill_directory_digest" not in computer.manifest
    assert computer.manifest["skill_directory_dirty"]
    monkeypatch.setattr(store, "put", actual_put)
    directory.synchronize(db, computer, access, packages, versions)
    assert store.objects == original


def test_failed_publication_never_creates_paid_environment_and_cleanup_retries(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, store = prepared(accounts, monkeypatch)
    scripted(client)
    command = submit(db, access, conversation)
    computer = db.get(cloud.Computer, command.session_id)
    actual_put = store.put
    def fail_after_first(key, source, *args):
        if store.objects: raise OSError("synthetic storage failure")
        return actual_put(key, source, *args)
    monkeypatch.setattr(store, "put", fail_after_first)
    cloud.run(command.id, factory)
    db.expire_all()
    assert command.status == "failed" and computer.ended_at
    assert fake.creates == fake.starts == fake.kills == 0
    assert computer.manifest["skill_directory_dirty"] and "skill_directory_digest" not in computer.manifest
    root = directory.prefix(access.workspace_id, access.user_id, computer.id)
    shared = computer.snapshot["resource_mount"]["prefix"] + "keep.txt"
    other = directory.prefix(access.workspace_id, uuid4(), uuid4()) + "private.txt"
    store.objects.update({shared: b"shared resource", other: b"other private package"})
    actual_delete = store.delete
    monkeypatch.setattr(store, "delete", lambda *_: (_ for _ in ()).throw(OSError("temporary delete failure")))
    cloud.maintenance(factory)
    db.expire_all()
    assert computer.manifest["skill_directory_dirty"]
    monkeypatch.setattr(store, "delete", actual_delete)
    cloud.maintenance(factory)
    db.expire_all()
    assert not computer.manifest.get("skill_directory_dirty")
    assert not any(key.startswith(root) for key in store.objects)
    assert store.objects == {shared: b"shared resource", other: b"other private package"}


def test_hidden_legacy_rows_cannot_be_listed_mentioned_attached_or_downloaded(accounts, monkeypatch, resource_store):
    client, db, _, access, _, _, _ = prepared(accounts, monkeypatch)
    public = upload(client, "public.txt")
    private = upload(client, "legacy.txt")
    row = db.get(WorkspaceFile, UUID(private["id"]))
    row.path = "SKILL-PACKAGES/private/SKILL.md"
    db.commit()
    for url in ("/resources", "/resources?q=SKILL", f"/workspaces/{access.workspace_id}/files"):
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert private["id"] not in json.dumps(response.json())
    response = client.get("/conversations/knowledge-catalog")
    assert response.status_code == 200, response.text
    assert private["id"] not in json.dumps(response.json())
    for suffix in ("", "/download", "/download?preview=true"):
        assert client.get(f"/resources/{private['id']}" + suffix).status_code == 404
    assert client.post(f"/resources/{private['id']}/attachment?version=1").status_code == 404
    assert client.delete(f"/resources/{private['id']}").status_code == 404
    assert client.get(f"/workspaces/{access.workspace_id}/files/{private['id']}").status_code == 404
    for path in ("skill-packages/x.txt", "SKILL-PACKAGES/x.txt", "skill-packages"):
        with pytest.raises(HTTPException, match="管理页"):
            workspace_files.valid_path(path)
    db.refresh(row)
    assert row.path == "SKILL-PACKAGES/private/SKILL.md" and not row.deleted_at
    assert db.get(WorkspaceFile, UUID(public["id"])).path == public["path"]


def test_file_tools_hide_managed_directory_but_allow_private_read(tmp_path):
    path = tmp_path / "skill-packages" / str(uuid4()) / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("private instruction", encoding="utf-8")
    (tmp_path / "result.txt").write_text("output", encoding="utf-8")
    assert [item["path"] for item in perform({"name": "list_files"}, tmp_path)["items"]] == ["result.txt"]
    relative = path.relative_to(tmp_path).as_posix()
    assert perform({"name": "read_file", "path": relative}, tmp_path)["content"] == "private instruction"
    for name in ("write_file", "apply_patch", "delete_file"):
        with pytest.raises(ValueError, match="管理页"):
            perform({"name": name, "path": relative, "content": "overwritten", "patch": ""}, tmp_path)
    assert path.read_text() == "private instruction"


@pytest.mark.skipif(os.environ.get("RAG_DOCKER_ACCEPT") != "1", reason="local Docker acceptance opt-in required")
def test_real_linux_nested_read_only_mount_executes_relative_resources_and_keeps_outputs(tmp_path):
    shared, private = tmp_path / "shared", tmp_path / "private"
    shared.mkdir(); private.mkdir()
    (shared / "skill-packages").mkdir()
    (shared / "skill-packages" / "other-user.txt").write_text("must not be visible")
    package = private / "own"
    (package / "scripts").mkdir(parents=True)
    (package / "assets").mkdir()
    (package / "assets" / "value.txt").write_text("PRIVATE VALUE")
    (package / "scripts" / "task.py").write_text(
        "from pathlib import Path\np=Path(__file__).resolve().parents[1]\n"
        "Path('result.txt').write_text((p/'assets/value.txt').read_text())\n")
    verification = """from pathlib import Path
import subprocess
root=Path('/home/user/workspace')
subprocess.run(['python', 'skill-packages/own/scripts/task.py'], check=True)
assert (root/'result.txt').read_text() == 'PRIVATE VALUE'
assert not (root/'skill-packages/other-user.txt').exists()
for target in [root/'skill-packages/own/assets/value.txt', root/'skill-packages/new.txt']:
    try: target.write_text('tampered')
    except OSError: pass
    else: raise AssertionError('managed mount was writable')
try: (root/'skill-packages/own/assets/value.txt').unlink()
except OSError: pass
else: raise AssertionError('managed file was removable')
print('READ_EXECUTE_PRIVATE_READONLY_OUTPUT_OK')
"""
    completed = subprocess.run(["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--mount", f"type=bind,source={shared},target=/home/user/workspace",
        "--mount", f"type=bind,source={private},target=/home/user/workspace/skill-packages,readonly",
        "-w", "/home/user/workspace", "python:3.12-slim", "python", "-c", verification],
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "READ_EXECUTE_PRIVATE_READONLY_OUTPUT_OK" in completed.stdout
    assert (shared / "result.txt").read_text() == "PRIVATE VALUE"
