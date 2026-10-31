"""Private package authorization and real remote MCP execution boundaries."""
import base64
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import conversations, mcp_client, workspace_files
from app.agent import AgentCancelled
from app.extensions import ExtensionRuntime
from app.llm import LLMUsage, ToolCall, ToolResponse
from app.models import AgentSkill, Conversation, MCPServer, RemoteToolExecution, Workspace, Membership, SiteModelProvider
from auth_helpers import StreamingTestProvider
from test_auth import register
from test_extensions import access_for
from test_shared_providers import setup_site, provider
from test_request_models import send
from test_site_skill_packages import zipped
from test_mcp_client import live_mcp

TOOL = {"name": "calculate", "description": "Calculate", "inputSchema": {"type": "object",
    "properties": {"a": {"type": "integer"}}, "required": ["a"], "additionalProperties": False}}


def skill(client, **changes):
    result = client.post("/agent/skills", json={"name": "Personal", "description": "Write a report",
        "markdown": "Read [guide](references/guide.md).", "enabled": True,
        "files": [{"path": "references/guide.md", "content": base64.b64encode(b"ONLY RESOURCE MARKER").decode()}],
        **changes})
    assert result.status_code == 201, result.text
    return result.json()


def connection(client):
    response = client.post("/agent/mcp", json={"name": "My service", "url": "https://mcp.example.com/mcp"})
    assert response.status_code == 201, response.text
    return response.json()


def update_connection(client, row, **changes):
    return client.put(f"/agent/mcp/{row['id']}", json={key: row[key] for key in
        ("name", "url", "transport", "enabled", "allowed_tools", "version")} | changes)


def test_personal_package_draft_limits_reading_ownership_and_conflicts(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    (_, member, anonymous), _ = accounts
    register(member, "personal-other@example.com")
    files = {"SKILL.md": b"---\nname: private\ndescription: Private method\n---\nRead the guide.",
        "references/guide.md": b"Private guide", "scripts/task.py": b"raise RuntimeError('NEVER RUN')",
        "assets/binary.png": b"\xff\x00"}
    imported = admin.post("/agent/skills/import", files={"file": ("method.zip", zipped(files, "method/"))})
    assert imported.status_code == 200, imported.text
    draft = imported.json()
    assert not draft["enabled"] and draft["review"]["has_limitations"]
    body = {key: draft[key] for key in ("name", "description", "markdown", "files")}
    assert admin.post("/agent/skills", json={**body, "enabled": True}).status_code == 422
    response = admin.post("/agent/skills", json={**body, "enabled": True, "limitations_acknowledged": True})
    assert response.status_code == 201, response.text
    row = response.json()
    assert len(row["files"]) == 3
    assert member.get(f"/agent/skills/{row['id']}").status_code == 404
    assert member.get("/agent/skills").json() == []
    assert anonymous.get("/agent/skills").status_code == 401
    access = access_for(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [row["id"]], [])
    args = {"skill_id": row["id"]}
    assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"}).get("error")
    loaded = runtime.execute("load_skill", args)
    assert loaded["resources"] == [{"path": "references/guide.md"}, {"path": "scripts/task.py"}]
    assert "NEVER RUN" in runtime.execute("read_skill_resource", {**args, "path": "scripts/task.py"})["content"]
    assert runtime.execute("read_skill_resource", {**args, "path": "assets/binary.png"}).get("error")
    assert runtime.execute("read_skill_resource", {**args, "path": "../x"}).get("error")
    assert runtime.execute("read_skill_resource", {**args, "path": "references/guide.md"})["content"] == "Private guide"
    update = admin.put(f"/agent/skills/{row['id']}", json={**body, "enabled": False, "version": 1})
    assert update.status_code == 200 and update.json()["version"] == 2
    assert admin.put(f"/agent/skills/{row['id']}", json={**body, "version": 1}).status_code == 409
    assert runtime.execute("load_skill", args).get("error")
    assert admin.get(f"/agent/skills/{row['id']}/export").status_code == 200


def test_folder_import_is_draft_and_does_not_publish(accounts, monkeypatch):
    admin, _, _ = setup_site(accounts, monkeypatch)
    response = admin.post("/agent/skills/import-folder", files=[
        ("files", ("SKILL.md", b"Instructions")), ("files", ("guide.txt", b"Guide"))],
        data={"paths": ["folder/SKILL.md", "folder/guide.txt"]})
    assert response.status_code == 200, response.text
    assert response.json()["files"][0]["path"] == "guide.txt"
    assert admin.get("/agent/skills").json() == []


def test_same_user_other_workspace_cannot_read_select_or_execute(accounts, monkeypatch):
    from dataclasses import replace
    admin, db, _ = setup_site(accounts, monkeypatch)
    method, remote = skill(admin), connection(admin)
    access = access_for(admin, db)
    workspace = Workspace(name="Second workspace")
    db.add(workspace); db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=access.user_id, role="admin"))
    db.commit()
    other = replace(access, workspace_id=workspace.id)
    from app.extensions import validate_selection
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        validate_selection(db, other, [method["id"]], [])
    assert exc.value.status_code == 404
    runtime = ExtensionRuntime(lambda: nullcontext(db), other, [method["id"]], [remote["id"]])
    assert runtime.execute("load_skill", {"skill_id":method["id"]}).get("error")
    assert runtime.bindings == {}
    assert admin.post("/agent/skills/import", files={"file":("bad.zip", zipped(
        {"SKILL.md":b"Read instructions", "../escape.txt":b"bad"}))}).status_code == 422


def test_model_without_tools_does_not_claim_extension_use(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    chosen = db.get(SiteModelProvider, UUID(model["id"]))
    chosen.thinking_config = {"kind":"unconfigured", "supports_tools":False}
    db.commit()
    skill(admin)
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


def test_request_fixed_skill_and_conversation_defaults_and_legacy_selection(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    first = skill(admin)
    chat = admin.post("/conversations", json={}).json()
    assert chat["skill_ids"] == [first["id"]]
    second = skill(admin, name="Second")
    assert admin.get(f"/conversations/{chat['id']}").json()["skill_ids"] == [first["id"]]
    access = access_for(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [first["id"]], [])
    row = db.get(AgentSkill, UUID(first["id"]))
    row.markdown, row.version = "Changed", 2
    db.commit()
    assert runtime.execute("load_skill", {"skill_id": first["id"]})["instructions"] == first["markdown"]
    assert runtime.execute("load_skill", {"skill_id": second["id"]}).get("error")
    current = db.get(Conversation, UUID(chat["id"]))
    current.active_request_id = uuid4(); db.commit()
    assert admin.put(f"/conversations/{chat['id']}/extensions", json={}).status_code == 409
    current.active_request_id = None
    current.extensions_initialized = False
    db.commit()
    assert admin.get(f"/conversations/{chat['id']}").json()["skill_ids"] == []
    assert admin.put(f"/conversations/{chat['id']}/extensions", json={}).status_code == 200
    assert admin.post("/conversations", json={"skill_ids": [], "mcp_server_ids": []}).json()["skill_ids"] == []


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
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]])
    name = next(iter(runtime.bindings))
    calls = []
    def invoke(*args):
        args[-1]()
        calls.append(args)
        return {"content": [{"type": "text", "text": "result"}]}
    monkeypatch.setattr(mcp_client, "call", invoke)
    assert runtime.execute(name, {"a": "wrong"}).get("error") and not calls
    assert runtime.execute(name, {"a": 1})["_extension"]["tool"] == name
    assert len(calls) == 1
    stored.authorization_version += 1; db.commit()
    assert runtime.execute(name, {"a": 2}).get("error") and len(calls) == 1
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]])
    def uncertain(*args):
        calls.append(args)
        raise mcp_client.MCPError("超时；未自动重试，请核对远端执行状态")
    monkeypatch.setattr(mcp_client, "call", uncertain)
    assert runtime.execute(name, {"a": 3})["uncertain"]
    assert runtime.execute(name, {"a": 3})["uncertain"]
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
    method = skill(admin)
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=1)
            results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
            if not results:
                return ToolResponse("Use method", [ToolCall("method", "load_skill", {"skill_id": method["id"]})], self.last_usage)
            if len(results) == 1:
                return ToolResponse(None, [ToolCall("read", "read_skill_resource", {"skill_id": method["id"], "path": "references/guide.md"})], self.last_usage)
            if len(results) == 2:
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
    assert [item["kind"] for item in final["tool_events"]] == ["skill", "skill", "mcp"]
    assert connection_args["state"]["calls"] == 1
    restored = admin.get(f"/conversations/{chat['id']}").json()["messages"][-1]
    assert restored["tool_events"] == final["tool_events"]
    saved = db.scalar(select(RemoteToolExecution))
    assert saved.status == "completed" and "result" not in saved.encrypted_result
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]],
        UUID(final["id"]), uuid4())
    alias = next(iter(runtime.bindings))
    assert runtime.execute(alias, {"a": 3, "b": 4})["structuredContent"] == {"result": 7}
    assert connection_args["state"]["calls"] == 1
    saved.encrypted_result, saved.status = None, "uncertain"
    db.commit()
    restarted = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [], [row["id"]],
        UUID(final["id"]), uuid4())
    assert restarted.execute(alias, {"a": 3, "b": 4})["uncertain"]
    assert connection_args["state"]["calls"] == 1


def test_generated_file_download_link_data_persists_with_chat_and_enforces_workspace(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    model = provider(owner)
    stored = {}
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, *_: stored.update({key: stream.read()}))
    monkeypatch.setattr(workspace_files, "download_fileobj", lambda key: __import__("io").BytesIO(stored[key]))
    access = access_for(owner, db)
    saved = workspace_files._save_bytes(access.workspace_id, access, db, "reports/answer.md",
        b"Downloadable result", "text/markdown")
    db.commit()
    monkeypatch.setattr(ExtensionRuntime, "execute", lambda self, name, arguments, **kwargs:
        saved if name == "write_file" else {"error": "Unexpected test call"})

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=5)
            assert any(item["function"]["name"] == "write_file" for item in tools)
            assert "点击该次工具活动下方的下载入口" in messages[0]["content"]
            previous = [item for item in messages if item["role"] == "tool"]
            if not previous:
                return ToolResponse(None, [ToolCall("write", "write_file", {
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
    download = owner.get(f"/workspaces/{session['workspace_id']}/files/{artifact['id']}/download")
    assert download.status_code == 200 and download.content == b"Downloadable result"
    (_, other, _), _ = accounts
    register(other, "output-isolation@example.com")
    assert other.get(f"/workspaces/{session['workspace_id']}/files/{artifact['id']}/download").status_code == 404
