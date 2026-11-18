"""Imports and file operations share one ordinary workspace resource directory."""
import hashlib
import io
import stat
import subprocess
import zipfile
from collections.abc import MutableMapping
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import cloud_computer, cloud_file_operation, extensions, shared_resources, workspace_files
from app.extensions import ExtensionRuntime
from app.models import AgentSkill, Membership, SiteSkill, WorkspaceFile
from test_auth import register, share, switch
from test_cloud_computer import enable_execution
from test_extensions import access_for
from test_resources import resource_store
from test_shared_providers import setup_site


def zipped(files, root=""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in files.items():
            archive.writestr(root + path, data)
    return output.getvalue()


FILES = {
    "SKILL.md": b"---\nname: sample\ndescription: total\n---\nRead [later](missing.md). Run scripts/total.py",
    "scripts/total.py": b"from pathlib import Path\np=Path(__file__).resolve().parent.parent\nprint(sum(map(int,(p/'assets/data.csv').read_text().split())))\n",
    "assets/data.csv": b"3 7 11",
    "assets/image.png": b"\x89PNG\r\n\x1a\n",
}


def importing(client, endpoint="/agent/skills/import", files=None):
    result = client.post(endpoint, files={"file": ("sample.zip", zipped(files or FILES, "project/method/"))})
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.parametrize("endpoint", ["/agent/skills/import"])
def test_import_preserves_bytes_in_normal_resources_without_body_copy(accounts, monkeypatch, resource_store, endpoint):
    client, db, _ = setup_site(accounts, monkeypatch)
    result = importing(client, endpoint)
    listed = client.get("/resources").json()["items"]
    assert {item["path"] for item in listed} == {f"{result['directory']}/{path}" for path in FILES}
    for item in result["items"]:
        assert item["source"] == "resource_upload"
        assert client.get(item["download_url"]).content == FILES[item["path"].removeprefix(result["directory"] + "/")]
    assert db.scalars(select(AgentSkill)).all() == []
    assert db.scalars(select(SiteSkill)).all() == []
    catalog = client.get("/agent/skills").json()
    assert len(catalog) == 1 and catalog[0]["path"].endswith("/SKILL.md")
    assert not any("instructions" in item for item in catalog)
    usage = client.get("/storage/allowances").json()
    assert usage["used_bytes"] == sum(map(len, FILES.values()))
    assert usage["breakdown"]["skills"] == 0
    second = importing(client)
    assert second["directory"] != result["directory"]


def test_folder_import_preserves_nested_structure_and_does_not_start_cloud(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(cloud_computer, "submit", lambda *_: pytest.fail("cloud started during upload"))
    result = client.post("/agent/skills/import-folder", files=[
        ("files", ("SKILL.md", FILES["SKILL.md"])),
        ("files", ("data.csv", b"2 40"))],
        data={"paths": ["project/report/SKILL.md", "project/report/assets/data.csv"]})
    assert result.status_code == 201, result.text
    body = result.json()
    assert {item["path"] for item in body["items"]} == {
        f"{body['directory']}/SKILL.md", f"{body['directory']}/assets/data.csv"}


def test_skill_file_list_refreshes_external_changes_and_keeps_workspace_scope(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    result = importing(client)
    item = next(item for item in result["items"] if item["path"].endswith("/SKILL.md"))
    access = access_for(client, db)
    root = result["directory"]
    objects = resource_store[0]
    objects[shared_resources.object_key(access.workspace_id, f"{root}/references/new.txt")] = b"external"
    objects.pop(shared_resources.object_key(access.workspace_id, f"{root}/assets/data.csv"))
    objects[shared_resources.object_key(access.workspace_id, f"{root}/.rag-directory")] = b""
    paths = {file["path"] for file in client.get(f"/agent/skills/{item['id']}/files").json()}
    assert f"{root}/references/new.txt" in paths
    assert f"{root}/assets/data.csv" not in paths
    assert not any(path.endswith(".rag-directory") for path in paths)
    (_, other, _), _ = accounts
    register(other, "skill-file-list-outsider@example.com")
    assert other.get(f"/agent/skills/{item['id']}/files").status_code == 404


def test_shared_members_can_read_edit_and_delete_but_other_workspaces_cannot(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    (_, member, anonymous), _ = accounts
    register(member, "skill-reader@example.com")
    result = importing(client)
    item = result["items"][0]
    assert member.get(f"/agent/skills/{item['id']}").status_code == 404
    assert member.get("/agent/skills").json() == []
    assert anonymous.get("/agent/skills").status_code == 401
    access = access_for(client, db)
    share(client, "skill-reader@example.com", "viewer")
    switch(member, str(access.workspace_id))
    assert member.get(item["download_url"]).content == FILES["SKILL.md"]
    assert len(member.get("/agent/skills").json()) == 1
    assert member.post("/agent/skills/import", files={"file": ("SKILL.md", b"read")}).status_code == 403
    assert member.delete(f"/resources/{item['id']}").status_code == 403
    membership = db.scalar(select(Membership).where(Membership.workspace_id == access.workspace_id,
        Membership.user_id == access_for(member, db).user_id))
    membership.role = "editor"
    db.commit()
    member_access = access_for(member, db)
    workspace_files._save_bytes(member_access.workspace_id, member_access, db, item["path"], b"changed", "text/markdown")
    assert client.get(item["download_url"]).content == b"changed"
    assert member.delete(f"/resources/{item['id']}").status_code == 204
    assert client.get(f"/agent/skills/{item['id']}").status_code == 404
    assert client.get("/agent/skills").json() == []


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", "a//file", "a\\file", "C:/file"])
def test_unsafe_unpack_paths_are_rejected(accounts, monkeypatch, resource_store, path):
    client, _, _ = setup_site(accounts, monkeypatch)
    # ZipInfo normalizes Windows separators on Windows; preserve the malicious bytes.
    normalized = path.replace("\\", "/")
    data = zipped({"SKILL.md": b"ok", normalized: b"bad"}).replace(normalized.encode(), path.encode())
    result = client.post("/agent/skills/import", files={"file": ("bad.zip", data)})
    assert result.status_code == 422
    assert resource_store[0] == {}


def test_unpack_rejects_links_duplicate_roots_and_limits(accounts, monkeypatch, resource_store):
    client, _, _ = setup_site(accounts, monkeypatch)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", b"ok")
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../secret")
    assert client.post("/agent/skills/import", files={"file": ("bad.zip", output.getvalue())}).status_code == 422
    for files in ({"a/SKILL.md": b"one", "b/SKILL.md": b"two"}, {"SKILL.md": b"one", "skill.md": b"two"}, {"README.md": b"none"}):
        assert client.post("/agent/skills/import", files={"file": ("bad.zip", zipped(files))}).status_code == 422
    monkeypatch.setattr("app.skill_files.MAX_FILE_BYTES", 1024)
    assert client.post("/agent/skills/import", files={"file": ("large.zip", zipped({"SKILL.md": b"x" * 1025}))}).status_code == 413
    assert resource_store[0] == {}


def test_capacity_is_checked_before_writes_and_failed_import_removes_partial_objects(accounts, monkeypatch, resource_store):
    from app.storage_models import StorageQuotaSettings
    client, db, _ = setup_site(accounts, monkeypatch)
    quota = StorageQuotaSettings(id=1, version=1, free_storage_bytes=1,
        free_download_bytes=100000, output_limit_bytes=100000, retention_days=3, packages=[], costs={})
    db.add(quota); db.commit()
    response = client.post("/agent/skills/import", files={"file": ("small.zip", zipped(FILES))})
    assert response.status_code == 413 and not resource_store[0]
    quota.free_storage_bytes = 100000
    db.commit()
    store = shared_resources.backend(db)
    original = store.put
    calls = []
    def fail(key, stream, content_type):
        calls.append(key)
        original(key, stream, content_type)
        if len(calls) == 2:
            raise RuntimeError("synthetic upload failure")
    monkeypatch.setattr(store, "put", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        importing(client)
    assert resource_store[0] == {}
    assert db.scalars(select(WorkspaceFile)).all() == []


class DiskObjects(MutableMapping):
    """Synthetic object storage backed by the same directory used for execution."""
    def __init__(self, root):
        self.root = root
    def __getitem__(self, key):
        try:
            return (self.root / key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None
    def __setitem__(self, key, data):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    def __delitem__(self, key):
        (self.root / key).unlink()
    def __iter__(self):
        return (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
    def __len__(self):
        return sum(1 for _ in self)


def test_existing_file_tools_read_run_modify_delete_same_files_in_container(accounts, monkeypatch, resource_store, tmp_path):
    from test_cloud_computer_mount import MemoryMount
    client, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(client, db)
    enable_execution(db, access, monkeypatch)
    objects = DiskObjects(tmp_path / "objects")
    storage = MemoryMount()
    storage.objects = objects
    monkeypatch.setattr(shared_resources, "backend", lambda _: storage)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, *_: objects.__setitem__(key, stream.read()))
    result = importing(client)
    root = objects.root / shared_resources.prefix(access.workspace_id)
    # Execute the production in-sandbox file operations against the one shared root.
    commands = []
    def local_execute(_db, _access, _conversation, _request, arguments, check_active=None):
        commands.append(arguments)
        if "file_operation" in arguments:
            return {"status": "completed", "computer_id": arguments["computer_id"],
                    "cloud_computer": {"id": "local"}, "command_id": str(uuid4()),
                    "file_result": {"value": cloud_file_operation.perform(arguments["file_operation"], root)}}
        completed = subprocess.run(arguments["command"], shell=True, cwd=root, capture_output=True, text=True, timeout=10)
        return {"status": "completed", "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    monkeypatch.setattr(cloud_computer, "execute", local_execute)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, request_id=uuid4())
    assert runtime.skills == [{"name": "sample", "description": "total",
        "manifest_path": f"{result['directory']}/SKILL.md", "directory": result["directory"]}]
    assert not any(item["function"]["name"] in {"load_skill", "read_skill_resource"} for item in runtime.tools)
    computer_id = "00000000-0000-4000-a000-000000000112"
    def run(name, arguments):
        arguments = {**arguments, "computer_id": computer_id}
        return runtime.execute(name, arguments, call_id=str(uuid4()))
    listing = run("list_files", {"query": "SKILL.md"})
    assert "items" in listing, listing
    main = next(item for item in listing["items"] if item["path"].endswith("/SKILL.md"))
    assert run("read_file", {"path": main["path"]})["content"].encode() == FILES["SKILL.md"]
    directory = result["directory"]
    command = f'for id in {directory}; do python "$id"/scripts/*.py > total.txt; done; cat total.txt'
    assert run("run_command", {"command": command})["stdout"].strip() == "21"
    assert run("write_file", {"path": f"{directory}/assets/data.csv", "content": "2 40"}).get("error") is None
    assert run("run_command", {"command": command})["stdout"].strip() == "42"
    assert (root / "total.txt").read_text().strip() == "42"
    assert run("delete_file", {"path": main["path"]})["deleted"]
    assert client.get("/agent/skills").json() == []
    assert run("read_file", {"path": main["path"]}).get("error")
    assert all("skills" not in command and "skill_packages" not in command for command in commands)
    assert not any("/file-operations/" in key for key in objects)
