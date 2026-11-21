import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app import cloud_computer as cloud, workspace_files
from app.cloud_file_operation import perform
from test_resources import resource_store, upload, access_for
from test_auth import register


def test_file_operations_share_current_content(tmp_path):
    content = "resume\noriginal\n"
    result = perform({"name": "write_file", "path": "resume.txt", "content": content}, tmp_path)
    assert result["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert perform({"name": "read_file", "path": "resume.txt"}, tmp_path)["content"] == content
    perform({"name": "apply_patch", "path": "resume.txt",
        "patch": "@@ -1,2 +1,2 @@\n resume\n-original\n+edited\n"}, tmp_path)
    with pytest.raises(RuntimeError):
        perform({"name": "apply_patch", "path": "resume.txt",
            "patch": "@@ -1,2 +1,2 @@\n resume\n-original\n+lost\n"}, tmp_path)
    assert (tmp_path/"resume.txt").read_text() == "resume\nedited\n"
    perform({"name": "write_file", "path": "resume.txt", "content": "latest"}, tmp_path)
    assert perform({"name": "read_file", "path": "resume.txt"}, tmp_path)["content"] == "latest"
    perform({"name": "delete_file", "path": "resume.txt"}, tmp_path)
    assert perform({"name": "list_files"}, tmp_path)["items"] == []


def test_path_traversal_rejected_and_existing_file_can_be_edited(tmp_path):
    (tmp_path/"present.txt").write_text("original")
    perform({"name": "write_file", "path": "present.txt", "content": "edited"}, tmp_path)
    for path in ("../escape", "/absolute", "a/../b", "a\\b"):
        with pytest.raises(ValueError):
            perform({"name": "read_file", "path": path}, tmp_path)
    assert (tmp_path/"present.txt").read_text() == "edited"


def test_file_tool_dispatch_never_downloads_body(accounts, resource_store, monkeypatch):
    (client, _, _), db = accounts
    register(client)
    item = upload(client)
    access = access_for(client, db)
    calls = []
    def forbidden(*args, **kwargs): pytest.fail("server body/cache read")
    monkeypatch.setattr("app.resource_cache.read_bytes", forbidden)
    monkeypatch.setattr("app.storage.download_fileobj", forbidden)
    def execute(db, actor, conversation, request, arguments, check):
        calls.append(arguments["file_operation"])
        return {"status": "completed", "file_result": {"value": {"content": "resource facts"}},
                "computer_id": "same-computer", "cloud_computer": {"id": "same-computer"}, "command_id": "command"}
    monkeypatch.setattr(cloud, "execute", execute)
    result = workspace_files.execute_tool("read_file", {"id": item["id"],
        "computer_id": "00000000-0000-4000-a000-000000000112"}, db, access,
        access.user_id, uuid4(), uuid4())
    assert result["content"] == "resource facts"
    assert result["file_path"] == "/home/user/workspace/" + item["path"]
    assert result["files"][0]["id"] == item["id"]
    assert result["files"][0]["url"] == f"/resources?file={item['id']}"
    assert result["files"][0]["availability"] == "available"
    assert calls[0]["path"] == item["path"] and "history" not in calls[0]
    assert not resource_store[1]


def test_binary_read_returns_mounted_path(tmp_path):
    path = tmp_path/"image.png"
    path.write_bytes(b"synthetic")
    assert perform({"name": "read_file", "path": "image.png", "binary": True}, tmp_path)["file_path"] == str(path)


def test_worker_does_not_scan_or_copy_on_completion(accounts, resource_store, monkeypatch):
    from test_cloud_computer import setup
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch)
    def forbidden(*args, **kwargs): pytest.fail("post-command file scan/copy")
    monkeypatch.setattr(fake, "manifest", forbidden)
    monkeypatch.setattr(fake, "read", forbidden)
    identifier = cloud.create_computer(db, access, conversation.id, uuid4(), {"name": "文件测试"})["computer_id"]
    command = cloud.submit(db, access, conversation.id, uuid4(), {"command": "touch result.txt", "computer_id": identifier})
    cloud.run(command.id, factory)
    db.refresh(command)
    assert command.status == "completed" and "generated_files" not in command.result
    client.post(f"/cloud-computers/{command.session_id}/end")
    cloud.maintenance(factory)
    assert fake.kills == 1 and not resource_store[1]


def test_file_delete_invalidates_resource(accounts, resource_store, monkeypatch):
    (client, _, _), db = accounts
    register(client)
    item = upload(client)
    access = access_for(client, db)
    def execute(db, actor, conversation, request, args, check):
        from app.shared_resources import object_key
        resource_store[0].pop(object_key(actor.user_id, item["path"]))
        return {"status": "completed", "file_result": {"value": {"deleted": True}},
            "computer_id": "computer", "cloud_computer": {"id": "computer"}, "command_id": "command"}
    monkeypatch.setattr(cloud, "execute", execute)
    result = workspace_files.execute_tool("delete_file", {"path": item["path"]}, db, access,
        access.user_id, uuid4(), uuid4())
    assert result["deleted"] is True
    assert result["files"][0]["id"] == item["id"]
    assert result["files"][0]["path"] == item["path"]
    assert result["files"][0]["availability"] == "missing"
    assert client.get(f"/resources/{item['id']}/download").status_code == 404
