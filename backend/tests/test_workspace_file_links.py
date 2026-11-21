from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import cloud_computer, shared_resources, workspace_files
from app.extensions import ExtensionRuntime
from app.models import WorkspaceFile, WorkspaceFileVersion
from app.workspace_file_links import get_file_link
from test_auth import register
from test_resources import resource_store, upload, access_for


def test_exact_object_link_without_scan_read_cloud_or_transfer(accounts, resource_store, monkeypatch):
    (client, _, _), db = accounts
    register(client)
    access = access_for(client, db)
    path = "成果/中文 演示.pptx"
    resource_store[0][shared_resources.object_key(access.user_id, path)] = b"created by command"
    def forbidden(*args, **kwargs):
        pytest.fail("获取链接不能扫描、下载或启动环境")
    monkeypatch.setattr(shared_resources, "refresh_index", forbidden)
    monkeypatch.setattr(cloud_computer, "execute", forbidden)
    from test_cloud_computer_mount import MemoryMount
    monkeypatch.setattr(MemoryMount, "iter_objects", forbidden)
    monkeypatch.setattr(MemoryMount, "download", forbidden)
    first = get_file_link(db, access, {"path": "/home/user/workspace/" + path})
    assert first["path"] == path and first["size_bytes"] == 18
    assert first["url"] == f"/resources?file={first['id']}"
    second = workspace_files.execute_tool("get_workspace_file_link", {"id": first["id"]},
        db, access, access.user_id)
    assert second["id"] == first["id"]
    assert len(list(db.scalars(select(WorkspaceFile)))) == 1
    assert not resource_store[1]


@pytest.mark.parametrize("arguments", [
    {}, {"path": "../other.png"}, {"path": "/tmp/chart.png"}, {"path": "/home/user/workspace-other/a"},
    {"path": "a/../b"}, {"path": "a\\b"}, {"id": "invalid"}, {"path": "a", "id": str(uuid4())},
])
def test_invalid_references_rejected_before_storage(accounts, resource_store, arguments, monkeypatch):
    (client, _, _), db = accounts
    register(client)
    monkeypatch.setattr(shared_resources, "backend", lambda *_: pytest.fail("无效路径不能访问存储"))
    with pytest.raises(HTTPException) as error:
        get_file_link(db, access_for(client, db), arguments)
    assert error.value.status_code == 422


def test_access_deletion_current_metadata_and_storage_failure(accounts, resource_store, monkeypatch):
    (client, other, _), db = accounts
    register(client)
    item = upload(client, "current.png", b"old")
    access = access_for(client, db)
    register(other, "other@example.com")
    with pytest.raises(HTTPException) as error:
        get_file_link(db, access_for(other, db), {"id": item["id"]})
    assert error.value.status_code == 404
    key = shared_resources.object_key(access.user_id, item["path"])
    resource_store[0][key] = b"new current content"
    assert get_file_link(db, access, {"id": item["id"]})["size_bytes"] == len(b"new current content")
    resource_store[0].pop(key)
    with pytest.raises(HTTPException) as error:
        get_file_link(db, access, {"id": item["id"]})
    assert error.value.status_code == 404
    monkeypatch.setattr(shared_resources, "backend", lambda *_: (_ for _ in ()).throw(RuntimeError("private secret")))
    with pytest.raises(HTTPException) as error:
        get_file_link(db, access, {"path": "a.png"})
    assert error.value.status_code == 503
    assert "secret" not in error.value.detail


@pytest.mark.parametrize("metadata", [{"Content-Length": "invalid"}, {"ETag": "missing-size"},
    {"Content-Length": 5, "Type": "symlink"}])
def test_invalid_object_metadata_cannot_deliver_link(accounts, resource_store, monkeypatch, metadata):
    (client, _, _), db = accounts
    register(client)
    from test_cloud_computer_mount import MemoryMount
    monkeypatch.setattr(MemoryMount, "head", lambda *_: metadata)
    with pytest.raises(HTTPException) as error:
        get_file_link(db, access_for(client, db), {"path": "untrusted.png"})
    assert error.value.status_code in (422, 503)
    assert not list(db.scalars(select(WorkspaceFile)))


def test_link_reuses_one_head_and_refreshes_fallback_storage(accounts, resource_store, monkeypatch):
    (client, _, _), db = accounts
    register(client)
    item = upload(client)
    access = access_for(client, db)
    from test_cloud_computer_mount import MemoryMount
    storage = MemoryMount()
    storage.objects = resource_store[0]
    storage.objects[shared_resources.object_key(access.user_id, item["path"])] = b"current bytes"
    calls = []
    head = storage.head
    storage.head = lambda key: calls.append(key) or head(key)
    monkeypatch.setattr(shared_resources, "backend", lambda _: None)
    monkeypatch.setattr("app.storage_backend.current_storage", lambda _: storage)
    linked = get_file_link(db, access, {"id": item["id"]})
    assert linked["size_bytes"] == len(b"current bytes") and linked["revision"] != item["revision"]
    assert len(calls) == 1


def test_link_tool_available_without_execution_and_private_to_account(accounts, resource_store):
    (client, other, _), db = accounts
    register(client)
    register(other, "no-file-access@example.com")
    item = upload(client)
    access = access_for(client, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access.revalidate(db))
    assert not runtime.execution_available
    names = {tool["function"]["name"] for tool in runtime.tools}
    assert "get_workspace_file_link" in names and "write_file" not in names
    result = runtime.execute("get_workspace_file_link", {"id": item["id"]})
    assert result["url"] == f"/resources?file={item['id']}"
    rejected = runtime.execute("get_workspace_file_link", {"id": item["id"], "path": item["path"]})
    assert rejected["error_code"] == "file_arguments"
    other_runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(other, db))
    assert other_runtime.execute("get_workspace_file_link", {"id": item["id"]})["error_code"] == "file_missing"


def test_download_and_file_link_use_the_recorded_legacy_key(accounts, resource_store):
    (client, other, _), db = accounts
    register(client)
    register(other, "legacy-key-other@example.com")
    item = upload(client, "merged.txt", b"retained bytes")
    access = access_for(client, db)
    version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == UUID(item["id"])))
    old_key = shared_resources.object_key(uuid4(), "original.txt")
    resource_store[0][old_key] = resource_store[0].pop(version.object_key)
    resource_store[0][version.object_key] = b"different bytes"
    version.object_key = old_key
    db.commit()

    assert client.get(f"/resources/{item['id']}/download").content == b"retained bytes"
    assert client.get(f"/files/{item['id']}/text").json()["content"] == "retained bytes"
    assert get_file_link(db, access, {"id": item["id"]})["path"] == "merged.txt"
    assert get_file_link(db, access, {"path": "merged.txt"})["id"] == item["id"]
    assert other.get(f"/resources/{item['id']}/download").status_code == 404
    assert version.object_key == old_key


def test_durable_command_link_delivery_and_replay(accounts, resource_store, monkeypatch):
    """The real Agent gets a link for a command-created file, then persists it."""
    import json
    from app import conversations
    from app.llm import ToolCall, ToolResponse
    from auth_helpers import configured_model, StreamingTestProvider
    from test_cloud_computer import enable_execution
    from test_conversations import create, turn
    from test_auth import register
    (http, _, _), db = accounts
    register(http)
    actor = access_for(http, db)
    configured_model(db, actor.user_id, monkeypatch)
    fake, _ = enable_execution(db, actor, monkeypatch)
    fake.outputs = {"成果/中文报告.pptx": b"command-created-pptx"}

    computer_id = None
    class Provider(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, **kwargs):
            observed = [item for item in messages if item["role"] == "tool"]
            if not observed:
                return ToolResponse(None, [ToolCall("make", "run_command", {
                    "computer_id": computer_id,
                    "command": "synthetic-create"})])
            if observed[-1]["name"] == "run_command":
                assert "generated_files" not in json.loads(observed[-1]["content"])
                return ToolResponse("文件已保存，正在获取入口。", [
                    ToolCall("link", "get_workspace_file_link", {"path": "成果/中文报告.pptx"})])
            result = json.loads(observed[-1]["content"])
            return ToolResponse(f"请查看 [中文报告.pptx]({result['url']})。", [])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda **_: Provider())
    conversation = create(http)
    from app import cloud_computer
    actor = access_for(http, db)
    computer_id = cloud_computer.create_computer(db, actor, UUID(conversation["id"]), uuid4(),
        {"name": "文件链接测试"})["computer_id"]
    events, request = turn(http, conversation["id"], "生成报告并给我文件链接")
    assert events[-1][1]["status"] == "completed"
    file_event = next(value for name, value in events if name == "tool_result"
        and value["name"] == "get_workspace_file_link")
    assert file_event["files"][0]["url"].startswith("/resources?file=")
    assert file_event["files"][0]["availability"] == "available"
    assert not ({"download_url", "object_key", "sha256"} & file_event["files"][0].keys())
    saved = http.get(f"/conversations/{conversation['id']}").json()["messages"][-1]
    assert saved["tool_events"][-1]["files"] == file_event["files"]
    assert file_event["files"][0]["url"] in saved["content"]
    replay = http.get(f"/conversations/{conversation['id']}/requests/{request['request_id']}/events")
    assert file_event["files"][0]["url"] in replay.text
    assert fake.creates == 1 and fake.starts == 1
