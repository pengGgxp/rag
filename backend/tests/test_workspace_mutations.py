"""Behavior regressions from the Issue #100 audit; all objects are local fixtures."""
import hashlib
from uuid import UUID, uuid4
import io
import zipfile

import pytest
from sqlalchemy import select

from app.files import operations as ops, shared, workspace as workspace_files
from app.models import WorkspaceFile, WorkspaceFileOperation, WorkspaceSkillInstall
from test_resources import resource_store, access_for, upload
from test_auth import register
from test_shared_providers import setup_site
from test_issue_100_skill_library import platform_skill


def setup(accounts):
    (client, _, _), db = accounts
    register(client)
    return client, db, access_for(client, db)


def write(client, actor, path, content, revision=None):
    return client.put("/files", json={"path": path, "content": content, "revision": revision})


@pytest.mark.parametrize("directory,neighbor", [("a_b", "axb"), ("a%b", "axxb"), ("a/b", "axb")])
def test_browse_uses_literal_directory_prefix(accounts, resource_store, directory, neighbor):
    client, db, actor = setup(accounts)
    for root in (directory, neighbor):
        assert write(client, actor, f"{root}/inside.txt", root).status_code == 200
        assert client.post("/resources/directories", json={"path": f"{root}/empty"}).status_code == 201
    result = client.get("/resources/browse", params={"path": directory})
    assert result.status_code == 200, result.text
    assert {item["path"] for item in result.json()["items"]} == {f"{directory}/inside.txt", f"{directory}/empty"}


@pytest.mark.parametrize("directory,neighbor", [("review_skill", "reviewXskill"), ("review%skill", "reviewXXskill"), ("review/skill", "reviewXskill")])
def test_skill_files_use_literal_directory_prefix(accounts, resource_store, directory, neighbor):
    client, db, actor = setup(accounts)
    manifests = {}
    for name in (directory, neighbor):
        root = f".skills/{name}"
        manifests[name] = write(client, actor, f"{root}/SKILL.md", "---\nname: example\n---\nbody").json()
        assert write(client, actor, f"{root}/inside.txt", name).status_code == 200
    result = client.get(f"/agent/skills/{manifests[directory]['id']}/files")
    assert result.status_code == 200, result.text
    assert {item["path"] for item in result.json()} == {f".skills/{directory}/SKILL.md", f".skills/{directory}/inside.txt"}


def test_file_rename_replays_original_response_after_path_changes(accounts, resource_store):
    client, db, actor = setup(accounts)
    item = upload(client, "before.txt", b"original")
    endpoint = f"/resources/files/{item['id']}"
    request = {"path": item["path"], "new_path": "after.txt", "revision": item["revision"], "request_id": str(uuid4())}
    first = client.put(endpoint, json=request)
    assert first.status_code == 200, first.text
    for _ in range(2):
        retry = client.put(endpoint, json=request)
        assert retry.status_code == 200, retry.text
        assert retry.json() == first.json()
    assert client.put(endpoint, json={**request, "new_path": "different.txt"}).status_code == 409
    assert client.put(endpoint, json={**request, "revision": "stale"}).status_code == 409
    assert client.put(endpoint, json={"new_path": "later.txt", "revision": first.json()["revision"], "request_id": str(uuid4())}).status_code == 200
    retry = client.put(endpoint, json=request)
    assert retry.status_code == 200 and retry.json() == first.json()
    assert client.get(f"/resources/{item['id']}").json()["path"] == "later.txt"
    assert resource_store[0][shared.object_key(actor.user_id, "later.txt")] == b"original"
    assert len(db.scalars(select(WorkspaceFileOperation).where(WorkspaceFileOperation.request_id == request["request_id"])).all()) == 1


def test_file_rename_replay_checks_account_and_file_identity(accounts, resource_store):
    (client, other, _), _db = accounts
    register(client)
    register(other, "rename-other@example.com")
    item = upload(client, "before.txt")
    other_item = upload(other, "before.txt", b"separate content")
    endpoint = f"/resources/files/{item['id']}"
    request = {"new_path": "after.txt", "revision": item["revision"], "request_id": str(uuid4())}
    first = client.put(endpoint, json=request)
    assert first.status_code == 200
    assert other.put(endpoint, json=request).status_code == 404
    second = upload(client, "second.txt")
    assert client.put(f"/resources/files/{second['id']}", json=request).status_code == 409
    assert other.get(f"/resources/{other_item['id']}/download").content == b"separate content"
    own_request = {**request, "revision": other_item["revision"]}
    own_endpoint = f"/resources/files/{other_item['id']}"
    assert other.put(own_endpoint, json=own_request).status_code == 200
    assert other.put(endpoint, json=request).status_code == 404


def test_file_rename_failed_commit_recovers_and_retries_same_request(accounts, resource_store, monkeypatch):
    client, db, actor = setup(accounts)
    item = upload(client, "before.txt", b"original")
    endpoint = f"/resources/files/{item['id']}"
    request = {"new_path": "after.txt", "revision": item["revision"], "request_id": str(uuid4())}
    commit, failed = db.commit, []
    def fail_once():
        if not failed and any(isinstance(row, WorkspaceFileOperation) and row.state == "completed" for row in db.dirty):
            failed.append(True)
            raise RuntimeError("synthetic rename commit failure")
        return commit()
    monkeypatch.setattr(db, "commit", fail_once)
    with pytest.raises(RuntimeError, match="rename commit"):
        client.put(endpoint, json=request)
    assert resource_store[0][shared.object_key(actor.user_id, "before.txt")] == b"original"
    assert shared.object_key(actor.user_id, "after.txt") not in resource_store[0]
    retry = client.put(endpoint, json=request)
    assert retry.status_code == 200, retry.text
    assert retry.json()["path"] == "after.txt"
    assert client.put(endpoint, json=request).json() == retry.json()
    assert not any("/file-operations/" in key for key in resource_store[0])


def test_file_rename_noop_is_replayable_without_moving_bytes(accounts, resource_store):
    client, db, actor = setup(accounts)
    item = upload(client, "same.txt")
    request = {"new_path": item["path"], "revision": item["revision"], "request_id": str(uuid4())}
    endpoint = f"/resources/files/{item['id']}"
    first = client.put(endpoint, json=request)
    assert first.status_code == 200 and first.json() == item
    assert client.put(endpoint, json=request).json() == first.json()
    assert len(resource_store[0]) == 1


@pytest.mark.parametrize("directory,neighbor", [("a_b", "axb"), ("a%b", "axxb")])
def test_attachment_directory_existence_uses_literal_prefix(accounts, resource_store, directory, neighbor):
    from fastapi import HTTPException, UploadFile
    from app.conversations.attachments import create_upload
    client, db, actor = setup(accounts)
    assert write(client, actor, f"{neighbor}/inside.txt", "neighbor").status_code == 200
    with pytest.raises(HTTPException) as error:
        create_upload(UploadFile(io.BytesIO(b"new"), filename="new.txt"), db, actor,
            source="chat_upload", directory=directory)
    assert error.value.status_code == 404
    assert write(client, actor, f"{directory}/inside.txt", "own").status_code == 200
    create_upload(UploadFile(io.BytesIO(b"new"), filename="new.txt"), db, actor,
        source="chat_upload", directory=directory)
    assert resource_store[0][shared.object_key(actor.user_id, f"{directory}/new.txt")] == b"new"


def test_revision_detects_external_same_size_change_and_preserves_new_bytes(accounts, resource_store):
    client, db, actor = setup(accounts)
    item = write(client, actor, "note.md", "first").json()
    read = client.get(f"/files/{item['id']}/text").json()
    resource_store[0][shared.object_key(actor.user_id, "note.md")] = b"other"
    assert write(client, actor, "note.md", "stale", read["revision"]).status_code == 409
    assert write(client, actor, "note.md", "no revision").status_code == 409
    current = client.get(f"/files/{item['id']}/text").json()
    assert current["content"] == "other" and current["revision"] != read["revision"]
    assert write(client, actor, "note.md", "saved", current["revision"]).status_code == 200


def test_browser_refreshes_files_and_empty_nested_directory_markers(accounts, resource_store):
    client, db, actor = setup(accounts)
    root = shared.prefix(actor.user_id)
    resource_store[0][root + "external/a.py"] = b"print(1)"
    resource_store[0][root + "empty/deep/.rag-directory"] = b""
    assert {item["path"] for item in client.get("/resources/browse").json()["items"]} == {"external", "empty"}
    assert client.get("/resources/browse?path=empty").json()["items"][0]["path"] == "empty/deep"
    resource_store[0].pop(root + "external/a.py")
    assert not client.get("/resources/browse?q=a.py").json()["items"]
    assert all(".rag-directory" not in item["path"] for item in client.get("/resources").json()["items"])
    assert client.get("/resources/browse?path=absent").status_code == 404


def test_implicit_rename_and_nested_markers_are_moved_together(accounts, resource_store):
    client, db, actor = setup(accounts)
    assert write(client, actor, "old/a.txt", "a").status_code == 200
    assert client.post("/resources/directories", json={"path": "old/empty"}).status_code == 201
    result = client.put("/resources/directories", json={"path": "old", "new_path": "new"})
    assert result.status_code == 200, result.text
    assert shared.directory_marker_key(actor.user_id, "new/empty") in resource_store[0]
    assert not any(key.startswith(shared.object_key(actor.user_id, "old/")) for key in resource_store[0])
    assert client.post("/resources/directories", json={"path": "occupied"}).status_code == 201
    assert client.put("/resources/directories", json={"path": "new", "new_path": "occupied"}).status_code == 409


def test_batch_preflight_and_delete_failure_restore_all_bytes(accounts, resource_store, monkeypatch):
    client, db, actor = setup(accounts)
    for path in ("docs/a.txt", "docs/b.txt"):
        assert write(client, actor, path, path).status_code == 200
    client.post("/resources/directories", json={"path": "target"})
    before = dict(resource_store[0])
    assert client.post("/resources/operations/move", json={"paths": ["docs/a.txt", "absent"], "destination": "target"}).status_code == 404
    assert resource_store[0] == before
    store = shared.backend(db)
    original = store.delete
    calls = []
    def fail_second(key):
        if key.startswith(shared.prefix(actor.user_id)):
            calls.append(key)
            if len(calls) == 2:
                raise RuntimeError("synthetic storage failure")
        return original(key)
    monkeypatch.setattr(store, "delete", fail_second)
    with pytest.raises(RuntimeError, match="synthetic"):
        client.request("DELETE", "/resources/directories", json={"path": "docs"})
    assert resource_store[0] == before
    assert len(client.get("/resources/browse?path=docs").json()["items"]) == 2
    assert db.scalar(select(WorkspaceFileOperation).where(WorkspaceFileOperation.state == "rolled_back"))


def test_conflicts_keep_both_replace_guard_and_request_retry(accounts, resource_store):
    client, db, actor = setup(accounts)
    upload(client, "same.txt", b"old")
    duplicate = client.post("/resources", files={"file": ("same.txt", b"new")})
    assert duplicate.status_code == 409
    revision = duplicate.json()["detail"]["conflicts"][0]["revision"]
    assert client.post("/resources", files={"file": ("same.txt", b"new")}, data={"conflict": "replace", "revision": "old"}).status_code == 409
    replaced = client.post("/resources", files={"file": ("same.txt", b"new")}, data={"conflict": "replace", "revision": revision})
    assert replaced.status_code == 201
    kept = client.post("/resources", files={"file": ("same.txt", b"both")}, data={"conflict": "keep_both"})
    assert kept.json()["path"] == "same (2).txt"
    request = {"paths": ["same.txt"], "destination": "", "conflict": "keep_both", "request_id": str(uuid4())}
    first = client.post("/resources/operations/copy", json=request)
    again = client.post("/resources/operations/copy", json=request)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    request["paths"] = ["same (2).txt"]
    assert client.post("/resources/operations/copy", json=request).status_code == 409
    identity = str(uuid4())
    kept = client.post("/resources", files={"file": ("same.txt", b"retry")}, data={"conflict": "keep_both", "request_id": identity})
    repeated = client.post("/resources", files={"file": ("same.txt", b"retry")}, data={"conflict": "keep_both", "request_id": identity})
    assert kept.status_code == repeated.status_code == 201 and kept.json() == repeated.json()


def test_deletion_confirmation_changes_and_root_path_guard(accounts, resource_store):
    client, db, actor = setup(accounts)
    assert write(client, actor, "docs/a.txt", "a").status_code == 200
    preview = client.post("/resources/operations/delete-preview", json={"paths": ["docs"]}).json()
    assert preview["files"] == preview["directories"] == 1
    assert write(client, actor, "docs/b.txt", "b").status_code == 200
    assert client.post("/resources/operations/delete", json={"paths": ["docs"], "confirmation": preview["confirmation"]}).status_code == 409
    for path in ("", "/", "../docs", "docs/../x", "bad?.txt"):
        assert client.request("DELETE", "/resources/directories", json={"path": path}).status_code == 422
    assert client.post("/resources/operations/move", json={"paths": ["docs"], "destination": "docs"}).status_code == 409


def test_scripts_binary_root_upload_and_directory_name_conflict(accounts, resource_store):
    client, db, actor = setup(accounts)
    assert upload(client, "run.py", b"print(1)")["path"] == "run.py"
    assert upload(client, "archive.zip", b"\x00\xff")["path"] == "archive.zip"
    assert client.post("/resources/directories", json={"path": "folder"}).status_code == 201
    assert client.post("/resources", files={"file": ("folder", b"x")}).status_code == 409


def test_install_cross_workspace_name_only_delete_and_reinstall(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    skill = platform_skill(db, UUID(client.get("/auth/me").json()["user"]["id"]))
    second = accounts[0][1]
    register(second, "second-install@example.com")
    endpoint = f"/agent/skills/platform/{skill.id}/install"
    request_id = str(uuid4())
    first = client.post(endpoint, headers={"X-Request-ID": request_id}).json()
    assert first["directory"].startswith(".skills/")
    assert second.post(endpoint).status_code == 201
    assert db.scalar(select(WorkspaceSkillInstall)) is None
    root = next(item for item in first["items"] if item["path"].endswith("/SKILL.md"))
    with zipfile.ZipFile(io.BytesIO(client.get(f"/agent/skills/platform/{skill.id}/download").content)) as package:
        assert package.read("SKILL.md") == client.get(root["download_url"]).content
        assert "name: 报告助手" in package.read("SKILL.md").decode()
    actor = access_for(client, db)
    text = f"---\nname: {skill.name}\n---\nchanged\n"
    assert write(client, actor, root["path"], text, root["revision"]).status_code == 200
    assert client.post(endpoint).json()["already_installed"]
    assert client.delete(f"/agent/skills/{root['id']}").status_code == 204
    assert not client.get(f"/agent/skills/platform/{skill.id}").json()["installed"]
    assert client.post(endpoint, headers={"X-Request-ID": request_id}).status_code == 409
    assert client.post(endpoint).status_code == 201


def test_discovery_duplicates_damage_roots_and_context_metadata(accounts, resource_store, monkeypatch):
    client, db, actor = setup(accounts)
    from app.skills.discovery import summary, prompt
    for path, text in {
        ".skills/a/SKILL.md": "---\nname: demo\ndescription: current\n---\nFULL BODY SECRET",
        ".skills/a/examples/SKILL.md": "---\nname: example\n---",
        ".skills/b/SKILL.md": "---\nname: demo\n---",
        ".skills/broken/SKILL.md": "no metadata",
        ".skills/invalid-description/SKILL.md": "---\nname: repair-me\ndescription: [invalid]\n---",
        "skills/old/SKILL.md": "---\nname: legacy\n---",
        ".skill/old/SKILL.md": "---\nname: legacy\n---"}.items():
        assert write(client, actor, path, text).status_code == 200
    found = client.get("/agent/skills").json()
    assert len(found) == 4 and sum(item["name"] == "demo" for item in found) == 2
    assert next(item for item in found if item["directory"].endswith("broken"))["manifest_error"]
    data = summary(db, actor)
    assert len(data) == 2 and "FULL BODY SECRET" not in prompt(data)
    assert all(item["manifest_path"].startswith(".skills/") for item in data)
    client.post("/resources/directories", json={"path": "archive"})
    assert client.post("/resources/operations/move", json={"paths": [".skills/a"], "destination": "archive"}).status_code == 200
    assert len(client.get("/agent/skills").json()) == 3


def test_interrupted_operation_recovers_but_keeps_new_external_content(accounts, resource_store):
    client, db, actor = setup(accounts)
    row = upload(client, "note.txt", b"before")
    key = shared.object_key(actor.user_id, row["path"])
    backup = f"accounts/{actor.user_id}/file-operations/test/before"
    resource_store[0][backup] = b"before"
    before = ops.revision(shared.backend(db).head(key))
    operation = WorkspaceFileOperation(account_id=actor.user_id, user_id=actor.user_id,
        request_id=str(uuid4()), fingerprint="x"*64, state="pending", result={}, steps=[{
            "key": key, "before": before, "backup": backup, "delete": False,
            "after_hash": hashlib.sha256(b"after").hexdigest()}])
    db.add(operation); db.commit()
    resource_store[0][key] = b"external newer"
    ops.recover_pending(db)
    assert operation.state == "recovery_required" and resource_store[0][key] == b"external newer"
    assert resource_store[0][backup] == b"before"
    resource_store[0][key] = b"after"
    ops.recover_pending(db)
    assert operation.state == "rolled_back" and resource_store[0][key] == b"before" and backup not in resource_store[0]


def test_metadata_commit_failure_compensates_and_original_request_retries(accounts, resource_store, monkeypatch):
    client, db, actor = setup(accounts)
    initial = upload(client, "commit.txt", b"original")
    key = shared.object_key(actor.user_id, "commit.txt")
    original = db.commit
    failed = []
    def fail_once():
        if not failed and any(isinstance(row, WorkspaceFileOperation) and row.state == "completed" for row in db.dirty):
            failed.append(True)
            raise RuntimeError("synthetic metadata failure")
        return original()
    monkeypatch.setattr(db, "commit", fail_once)
    request = str(uuid4())
    with pytest.raises(RuntimeError, match="metadata"):
        from app.files.mutations import save_files
        save_files(db, actor, {"commit.txt": (b"changed", "text/plain")}, expected={"commit.txt": initial["revision"]}, request_id=request)
    assert resource_store[0][key] == b"original" and not any("/file-operations/" in name for name in resource_store[0])
    retry = save_files(db, actor, {"commit.txt": (b"changed", "text/plain")}, expected={"commit.txt": initial["revision"]}, request_id=request)
    assert retry["items"][0]["size_bytes"] == 7 and resource_store[0][key] == b"changed"


def test_directory_merge_requires_each_file_revision_and_preserves_empty_dirs(accounts, resource_store):
    client, db, actor = setup(accounts)
    for path, value in (("source/common.txt", "source"), ("target/source/common.txt", "target")):
        assert write(client, actor, path, value).status_code == 200
    assert client.post("/resources/directories", json={"path": "source/empty"}).status_code == 201
    request = {"paths": ["source"], "destination": "target", "conflict": "replace"}
    conflict = client.post("/resources/operations/move", json=request)
    assert conflict.status_code == 409
    item = conflict.json()["detail"]["conflicts"][0]
    request["expected"] = {item["path"]: item["revision"]}
    result = client.post("/resources/operations/move", json=request)
    assert result.status_code == 200, result.text
    assert shared.directory_marker_key(actor.user_id, "target/source/empty") in resource_store[0]
    assert resource_store[0][shared.object_key(actor.user_id, "target/source/common.txt")] == b"source"
    assert not any(name.startswith(shared.object_key(actor.user_id, "source/")) for name in resource_store[0])


def test_admin_import_maintains_source_package_without_installing(accounts, resource_store, monkeypatch):
    client, db, _ = setup_site(accounts, monkeypatch)
    from test_skill_resources import FILES, zipped
    preview = client.post("/site/admin/skills/import", files={"file": ("sample.zip", zipped(FILES))})
    assert preview.status_code == 200, preview.text
    assert db.scalar(select(WorkspaceFile)) is None and not resource_store[0]
    payload = {key: value for key, value in preview.json().items() if key != "package_review"}
    created = client.post("/site/admin/skills", json=payload)
    assert created.status_code == 201, created.text
    from app.models import SiteSkill
    row = db.get(SiteSkill, UUID(created.json()["id"]))
    assert {item.path: item.content for item in row.files} == {path: data for path, data in FILES.items() if path != "SKILL.md"}
    assert db.scalar(select(WorkspaceFile)) is None and not resource_store[0]
