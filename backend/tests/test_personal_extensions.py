"""Private package authorization and real remote MCP execution boundaries."""
import base64
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import mcp_client, workspace_files
from app.conversations import execution as conversations
from app.agent.loop import AgentCancelled
from app.extensions import ExtensionRuntime
from app.llm import LLMUsage, ToolCall, ToolResponse
from app.models import AgentSkill, Conversation, MCPServer, RemoteToolExecution, SiteModelProvider
from auth_helpers import StreamingTestProvider
from test_auth import register
from test_extensions import access_for
from test_shared_providers import setup_site, provider
from test_request_models import send
from test_site_skill_packages import zipped
from test_mcp_client import live_mcp
from remote_receipt_helpers import answer_identity

TOOL = {"name": "calculate", "description": "Calculate", "inputSchema": {"type": "object",
    "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}}


def connection(client):
    response = client.post("/agent/mcp", json={"name": "My service", "url": "https://mcp.example.com/mcp"})
    assert response.status_code == 201, response.text
    return response.json()


def update_connection(client, row, **changes):
    return client.put(f"/agent/mcp/{row['id']}", json={key: row[key] for key in
        ("name", "url", "transport", "enabled", "allowed_tools", "version")} | changes)


def load_tool(runtime, alias):
    assert runtime.execute("load_mcp_tool", {"alias": alias}).get("loaded_alias") == alias
    runtime.model_tools([])


def test_other_user_cannot_read_select_or_execute(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    remote = connection(admin)
    (_, other_client, _), _ = accounts
    register(other_client, "other-extension-account@example.com")
    other = access_for(other_client, db)
    from app.extensions import validate_selection
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        validate_selection(db, other, [], [remote["id"]])
    assert exc.value.status_code == 404
    runtime = ExtensionRuntime(lambda: nullcontext(db), other, [], [remote["id"]])
    assert runtime.bindings == {}
    assert admin.post("/agent/skills/import", files={"file":("bad.zip", zipped(
        {"SKILL.md":b"Read instructions", "../escape.txt":b"bad"}))}).status_code == 422


def test_model_without_tools_does_not_claim_extension_use(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    chosen = db.get(SiteModelProvider, UUID(model["id"]))
    chosen.thinking_config = {"kind":"unconfigured", "supports_tools":False}
    db.commit()
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            assert tools == []
            assert "Personal" not in messages[0]["content"]
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=2)
            return ToolResponse("Ordinary answer", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    chat = admin.post("/conversations",json={}).json()
    events,_ = send(admin,chat["id"],model["id"])
    final = events[-1].json()
    assert final["status"] == "completed", {key: final.get(key) for key in ("status", "error_detail", "tool_events")}
    assert not final["tool_events"]


def test_mcp_authorization_versions_discovery_failures_and_credentials(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    (_, member, _), _ = accounts
    register(member, "mcp-other@example.com")
    row = connection(admin)
    assert member.get(f"/agent/mcp/{row['id']}").status_code == 404
    assert update_connection(admin, row, enabled=True).status_code == 422
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [TOOL])
    row = admin.post(f"/agent/mcp/{row['id']}/sync", json={"version": row["version"]}).json()
    assert not row["enabled"] and not row["allowed_tools"]
    assert update_connection(admin, row, allowed_tools=["calculate"], enabled=True).status_code == 422
    response = update_connection(admin, row, allowed_tools=["calculate"], enabled=True, acknowledge_external_access=True)
    assert response.status_code == 200, response.text
    allowed = response.json()
    renamed = update_connection(admin, allowed, name="Renamed").json()
    assert renamed["authorization_version"] == allowed["authorization_version"]
    assert update_connection(admin, allowed).status_code == 409
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [{**TOOL, "description": "Changed"}])
    synced = admin.post(f"/agent/mcp/{row['id']}/sync", json={"version": renamed["version"]}).json()
    assert synced["allowed_tools"] == [] and not synced["enabled"]
    assert admin.post(f"/agent/mcp/{row['id']}/sync", json={"version": 1}).status_code == 409
    monkeypatch.setattr(mcp_client, "discover", lambda *_: (_ for _ in ()).throw(mcp_client.MCPError("Unavailable")))
    assert admin.post(f"/agent/mcp/{row['id']}/sync", json={"version": synced["version"]}).status_code == 502
    assert admin.get(f"/agent/mcp/{row['id']}").json()["version"] == synced["version"]
    from cryptography.fernet import Fernet
    settings = mcp_client.get_settings().model_copy(update={"mcp_credential_key": Fernet.generate_key().decode()})
    from pydantic import SecretStr
    settings.mcp_credential_key = SecretStr(settings.mcp_credential_key)
    monkeypatch.setattr(mcp_client, "get_settings", lambda: settings)
    changed = update_connection(admin, synced, headers={"Authorization": "Bearer secret"}).json()
    assert changed["has_credentials"] and "secret" not in str(changed)
    stored = db.get(MCPServer, UUID(row["id"]), populate_existing=True)
    assert "secret" not in stored.encrypted_headers


def test_runtime_mcp_checks_revocation_input_and_no_uncertain_replay(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    row = connection(admin)
    stored = db.get(MCPServer, UUID(row["id"]))
    stored.tools, stored.allowed_tools, stored.enabled = [TOOL], ["calculate"], True
    stored.last_checked_at = datetime.now(timezone.utc)
    db.commit()
    message, request = answer_identity(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]], message, request)
    name = next(iter(runtime.bindings))
    load_tool(runtime, name)
    calls = []
    def invoke(*args):
        args[-1]()
        calls.append(args)
        return {"content": [{"type": "text", "text": "result"}]}
    monkeypatch.setattr(mcp_client, "call", invoke)
    assert runtime.execute(name, {"a": "wrong"}).get("error") and not calls
    assert runtime.execute(name, {"a": 1}, call_id="first")["_extension"]["tool"] == name
    assert len(calls) == 1
    stored.authorization_version += 1; db.commit()
    assert runtime.execute(name, {"a": 2}).get("error") and len(calls) == 1
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]], message, request)
    load_tool(runtime, name)
    def uncertain(*args):
        calls.append(args)
        raise mcp_client.MCPError("超时；未自动重试，请核对远端执行状态")
    monkeypatch.setattr(mcp_client, "call", uncertain)
    assert runtime.execute(name, {"a": 3}, call_id="unknown")["uncertain"]
    assert runtime.execute(name, {"a": 3}, call_id="unknown")["uncertain"]
    assert len(calls) == 2
    with pytest.raises(AgentCancelled):
        runtime.execute(name, {"a": 4}, lambda: (_ for _ in ()).throw(AgentCancelled()))


def test_real_http_mcp_in_runtime_and_streaming_conversation(accounts, monkeypatch, live_mcp):
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    connection_args = live_mcp
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    settings = mcp_client.get_settings().model_copy(update={
        "mcp_credential_key": SecretStr(Fernet.generate_key().decode())})
    monkeypatch.setattr(mcp_client, "get_settings", lambda: settings)
    response = admin.post("/agent/mcp", json={"name": "Calculator", "url": connection_args["url"],
        "transport": connection_args["transport"], "headers": connection_args["headers"]})
    assert response.status_code == 201, response.text
    row = response.json()
    synced = admin.post(f"/agent/mcp/{row['id']}/sync", json={"version": row["version"]})
    assert synced.status_code == 200, synced.text
    row = update_connection(admin, synced.json(), allowed_tools=["calculate"], enabled=True, acknowledge_external_access=True).json()
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=1)
            results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
            if not results:
                assert not any(tool["function"]["name"].startswith("mcp_") for tool in tools)
                return ToolResponse(None, [ToolCall("find", "search_mcp_tools", {"query": "calculate"})], self.last_usage)
            if messages[-1]["name"] == "search_mcp_tools":
                alias = results[-1]["tools"][0]["alias"]
                return ToolResponse(None, [ToolCall("load", "load_mcp_tool", {"alias": alias})], self.last_usage)
            if messages[-1]["name"] == "load_mcp_tool":
                alias = next(tool["function"]["name"] for tool in tools if tool["function"]["name"].startswith("mcp_"))
                return ToolResponse("Calculate next", [ToolCall("calc", alias, {"a": 3, "b": 4})], self.last_usage)
            assert results[-1]["structuredContent"] == {"result": 7}
            return ToolResponse("7. ONLY RESOURCE MARKER", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    chat = admin.post("/conversations", json={}).json()
    events, _ = send(admin, chat["id"], model["id"])
    final = events[-1].json()
    assert final["status"] == "completed", final
    assert final["content"].endswith("7. ONLY RESOURCE MARKER")
    assert [item["kind"] for item in final["tool_events"]] == ["mcp"] * 3
    assert connection_args["state"]["calls"] == 1
    restored = admin.get(f"/conversations/{chat['id']}").json()["messages"][-1]
    assert restored["tool_events"] == final["tool_events"]
    saved = db.scalar(select(RemoteToolExecution))
    assert saved.status == "completed" and "result" not in saved.encrypted_result
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]],
        UUID(final["id"]), saved.request_id)
    alias = next(iter(runtime.bindings))
    load_tool(runtime, alias)
    assert runtime.execute(alias, {"a": 3, "b": 4}, agent_tool_execution_id=saved.invocation_id)["structuredContent"] == {"result": 7}
    assert connection_args["state"]["calls"] == 1
    saved.encrypted_result, saved.status = None, "uncertain"
    db.commit()
    restarted = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]],
        UUID(final["id"]), saved.request_id)
    load_tool(restarted, alias)
    assert restarted.execute(alias, {"a": 3, "b": 4}, agent_tool_execution_id=saved.invocation_id)["uncertain"]
    assert connection_args["state"]["calls"] == 1


def test_generated_file_download_link_data_persists_with_chat_and_enforces_workspace(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    model = provider(owner)
    from test_cloud_computer import enable_execution
    enable_execution(db, access_for(owner, db), monkeypatch)
    db.commit()
    stored = {}
    from test_cloud_computer_mount import MemoryMount
    storage = MemoryMount()
    storage.objects = stored
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: storage)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, *_: stored.update({key: stream.read()}))
    monkeypatch.setattr(workspace_files, "download_fileobj", lambda key: __import__("io").BytesIO(stored[key]))
    from app import storage_downloads
    monkeypatch.setattr(storage_downloads, "download_range_fileobj",
        lambda key, start, end: __import__("io").BytesIO(stored[key][start:end+1]))
    access = access_for(owner, db)
    saved = workspace_files._save_bytes(access.user_id, access, db, "reports/answer.md",
        b"Downloadable result", "text/markdown")
    db.commit()
    monkeypatch.setattr(ExtensionRuntime, "execute", lambda self, name, arguments, **kwargs:
        saved if name == "write_file" else {"error": "Unexpected test call"})

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=5)
            assert any(item["function"]["name"] == "write_file" for item in tools)
            assert "get_workspace_file_link" in messages[0]["content"]
            previous = [item for item in messages if item["role"] == "tool"]
            if not previous:
                return ToolResponse(None, [ToolCall("write", "write_file", {
                    "computer_id": "00000000-0000-4000-a000-000000000112",
                    "path": "reports/answer.md", "content": "Downloadable result"})], self.last_usage)
            return ToolResponse("已生成 reports/answer.md，可点击本次操作下方的下载入口。", [], self.last_usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    conversation = owner.post("/conversations", json={}).json()["id"]
    events, _ = send(owner, conversation, model["id"])
    final = events[-1].json()
    assert final["status"] == "completed", final
    artifact = next(item for item in final["tool_events"] if item["name"] == "write_file")["files"][0]
    assert artifact["path"] == "reports/answer.md" and artifact["size_bytes"] == len(b"Downloadable result")
    assert "object_key" not in json.dumps(final["tool_events"])
    restored = owner.get(f"/conversations/{conversation}").json()["messages"][-1]
    assert restored["tool_events"] == final["tool_events"]
    download = owner.get(f"/files/{artifact['id']}/download")
    assert download.status_code == 200 and download.content == b"Downloadable result"
    (_, other, _), _ = accounts
    register(other, "output-isolation@example.com")
    assert other.get(f"/files/{artifact['id']}/download").status_code == 404


def test_mcp_new_invocation_observes_changes_and_replays_only_its_own_receipt(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    row = connection(admin)
    stored = db.get(MCPServer, UUID(row["id"]))
    stored.tools = [{**TOOL, "annotations": {"readOnlyHint": True}}]
    stored.allowed_tools, stored.enabled = ["calculate"], True
    stored.last_checked_at = datetime.now(timezone.utc)
    db.commit()
    message, request = answer_identity(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]], message, request)
    alias = next(iter(runtime.bindings))
    load_tool(runtime, alias)
    calls = []
    def invoke(*args):
        calls.append(args)
        return {"value": len(calls)}
    monkeypatch.setattr(mcp_client, "call", invoke)
    for index in range(25):
        assert runtime.execute(alias, {"a": 1}, call_id=str(index))["value"] == index + 1
    assert runtime.execute(alias, {"a": 1}, call_id="0")["value"] == 1
    assert runtime.execute(alias, {"a": 2}, call_id="0")["error_code"] == "tool_receipt"
    assert len(calls) == 25
