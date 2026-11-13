"""Platform text methods replace personal bundles; private data is never promoted."""
import json
from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from httpx_sse import EventSource
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, conversations
from app.conversation_context import scope_digest
from app.extensions import ExtensionRuntime
from app.llm import LLMUsage, ToolCall, ToolResponse
from app.models import AgentSkill, AgentSkillFile, Conversation, LoginSession, MCPServer, SiteAuditEvent, SiteSkill
from auth_helpers import StreamingTestProvider
from test_auth import register
from test_shared_providers import setup_site, provider
from test_request_models import send


def create(client, **changes):
    result = client.post("/site/admin/skills", json={"name": "报告方法", "description": "整理报告时使用",
        "markdown": "Use headings: Findings, Evidence, Next steps.", "enabled": True, **changes})
    assert result.status_code == 201, result.text
    return result.json()


def access_for(client, db):
    session = db.get(LoginSession, auth.digest(client.cookies.get("rag_session")))
    return auth.get_access(session, db)


def test_platform_resource_followup_uses_current_scope_without_load_flag(accounts, monkeypatch):
    import base64
    admin, db, _ = setup_site(accounts, monkeypatch)
    item = create(admin, files=[{"path": "guide.txt", "content": base64.b64encode(b"Platform guide").decode()}])
    access = access_for(admin, db)
    previous = ExtensionRuntime(lambda: nullcontext(db), access)
    assert "instructions" in previous.execute("load_skill", {"skill_id": item["id"]})
    current = ExtensionRuntime(lambda: nullcontext(db), access)
    args = {"skill_id": item["id"], "path": "guide.txt"}
    assert current.execute("read_skill_resource", args)["content"] == "Platform guide"
    assert current.execute("read_skill_resource", {**args, "path": "../guide.txt"}).get("error")
    assert current.execute("read_skill_resource", {**args, "offset": -1}).get("error")
    admin.put(f"/site/admin/skills/{item['id']}", json={**{key:item[key] for key in
        ("name", "description", "markdown", "version")}, "enabled": False})
    assert current.execute("read_skill_resource", args).get("error")


def test_admin_crud_import_permissions_conflicts_and_audit(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    (_, member, anonymous), _ = accounts
    register(member, "member@example.com")
    assert anonymous.get("/site/admin/skills").status_code == 401
    assert member.get("/site/admin/skills").status_code == 403
    item = create(admin, enabled=False)
    assert admin.get("/site/admin/skills?enabled=true").json()["total"] == 0
    assert admin.get("/site/admin/skills?q=报告").json()["total"] == 1
    assert "markdown" not in admin.get("/site/admin/skills").json()["items"][0]
    assert member.get(f"/site/admin/skills/{item['id']}").status_code == 403
    updated = admin.put(f"/site/admin/skills/{item['id']}", json={k: item[k] for k in
        ("name", "description", "markdown", "enabled", "version")})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    body = {k: item[k] for k in ("name", "description", "markdown", "enabled", "version")}
    assert admin.put(f"/site/admin/skills/{item['id']}", json=body).status_code == 409
    assert admin.request("DELETE", f"/site/admin/skills/{item['id']}", json={"version": 1}).status_code == 409
    markdown = "---\nname: writer\ndescription: Write reports\n---\n\n# Task\nWrite carefully."
    imported = admin.post("/site/admin/skills/import", files={"file": ("SKILL.md", markdown.encode())})
    assert imported.status_code == 200 and imported.json()["name"] == "writer"
    assert imported.json()["markdown"] == "# Task\nWrite carefully."
    assert imported.json()["enabled"] is False
    plain = admin.post("/site/admin/skills/import", files={"file": ("plain.md", b"# Plain instructions")})
    assert plain.status_code == 200 and plain.json()["description"] == ""
    assert admin.get("/site/admin/skills").json()["total"] == 1  # Import only fills a draft.
    for name, content in (("file.zip", b"bad"), ("file.md", b"\xff")):
        assert admin.post("/site/admin/skills/import", files={"file": (name, content)}).status_code == 422
    assert admin.post("/site/admin/skills/import", files={"file": ("file.md", b"a" * 210001)}).status_code == 413
    assert admin.request("DELETE", f"/site/admin/skills/{item['id']}", json={"version": 2}).status_code == 204
    assert admin.get(f"/site/admin/skills/{item['id']}").status_code == 404
    events = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.target_type == "site_skill")).all()
    assert len(events) == 3 and "Findings" not in str([row.details for row in events])


@pytest.mark.parametrize("change", [{"name": " "}, {"name": "x" * 65}, {"description": "x" * 1025},
    {"markdown": " "}, {"markdown": "x" * 50001}, {"files": [{"path": "../x", "content": "eA=="}]}])
def test_skill_validation(accounts, monkeypatch, change):
    admin, _, _ = setup_site(accounts, monkeypatch)
    body = {"name": "test", "description": "test", "markdown": "test", **change}
    assert admin.post("/site/admin/skills", json=body).status_code == 422


def test_runtime_snapshot_revocation_and_private_retention(accounts, monkeypatch):
    admin, db, identity = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    private = AgentSkill(workspace_id=access.workspace_id, owner_id=access.user_id, name="private",
        description="private", markdown="SECRET", files=[AgentSkillFile(path="secret.txt", content=b"PRIVATE")])
    db.add(private)
    db.add(MCPServer(workspace_id=access.workspace_id, owner_id=access.user_id, name="private-mcp",
        url="https://example.com", transport="sse", enabled=True, header_names=[], tools=[], allowed_tools=[]))
    db.commit()
    item = create(admin)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access)
    assert {tool["function"]["name"] for tool in runtime.tools} == {
        "load_skill", "read_skill_resource",
        "list_writable_knowledge_bases", "save_to_knowledge_base", "get_saved_document_status",
        "create_knowledge_base", "update_knowledge_base", "list_knowledge_bases", "get_knowledge_base_document_status"}
    assert "SECRET" not in str(runtime.skills) and "instructions" not in str(runtime.skills)
    args = {"skill_id": item["id"]}
    row = db.get(SiteSkill, UUID(item["id"]))
    row.markdown, row.version = "CHANGED", 2
    db.commit()
    assert runtime.execute("load_skill", args)["instructions"] == item["markdown"]
    assert runtime.execute("load_skill", args)["resources"] == []
    assert runtime.execute("load_skill", args)["version"] == 1
    assert runtime.execute("load_skill", {"skill_id": str(private.id)})["instructions"] == "SECRET"
    assert runtime.execute("read_skill_file", args).get("error")
    assert runtime.execute("mcp_anything", args).get("error")
    assert runtime.execute("load_skill", {**args, "extra": True}).get("error")
    row.enabled = False
    db.commit()
    assert runtime.execute("load_skill", args).get("error")
    db.delete(row)
    db.commit()
    assert runtime.execute("load_skill", args).get("error")
    from app.agent import AgentCancelled
    with pytest.raises(AgentCancelled):
        runtime.execute("load_skill", args, lambda: (_ for _ in ()).throw(AgentCancelled()))
    assert db.get(AgentSkill, private.id).files[0].content == b"PRIVATE"
    assert admin.get("/agent/skills").status_code == 200
    assert admin.get("/agent/mcp").status_code == 200
    assert admin.post("/conversations", json={"skill_ids": [str(private.id)]}).status_code == 201
    chat = admin.post("/conversations", json={}).json()
    assert admin.put(f"/conversations/{chat['id']}/extensions", json={}).status_code == 200


def test_agent_automatically_discovers_loads_and_records_method(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    item = create(admin)
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            assert "read_skill_file" not in str(tools) and "mcp_" not in str(tools)
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=1)
            if messages[-1]["role"] == "user":
                assert "整理报告时使用" in messages[0]["content"]
                assert "Use headings:" not in messages[0]["content"]
                return ToolResponse(None, [ToolCall("method", "load_skill", {"skill_id": item["id"]})], self.last_usage)
            result = json.loads(messages[-1]["content"])
            assert result["instructions"] == item["markdown"]
            return ToolResponse("Findings\nEvidence\nNext steps", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    chat = admin.post("/conversations", json={}).json()
    events, _ = send(admin, chat["id"], model["id"])
    final = events[-1].json()
    assert final["status"] == "completed", final
    event = final["tool_events"][0]
    assert event["skill_id"] == item["id"] and event["skill_version"] == 1
    assert "Use headings" not in str(event)
    assert "已读取「报告方法」的方法" == event["summary"]
    assert admin.get(f"/conversations/{chat['id']}").json()["messages"][-1]["tool_events"] == final["tool_events"]
    access = access_for(admin, db)
    conversation = db.get(Conversation, UUID(chat["id"]))
    before = scope_digest(db, conversation, access)
    pinned = [(item["id"], 1)]
    assert scope_digest(db, conversation, access, pinned) == before
    db.get(SiteSkill, UUID(item["id"])).version += 1
    db.commit()
    assert scope_digest(db, conversation, access) != before
    assert scope_digest(db, conversation, access, pinned) == before


def test_non_tool_model_ignores_platform_and_historical_extensions(accounts, monkeypatch):
    from app.models import SiteModelProvider
    from test_thinking import effort
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    create(admin)
    row = db.get(SiteModelProvider, UUID(model["id"]))
    row.thinking_config = {**effort("openai"), "supports_tools": False}
    row.thinking_version = 1
    chat = admin.post("/conversations", json={}).json()
    conversation = db.get(Conversation, UUID(chat["id"]))
    conversation.skill_ids, conversation.mcp_server_ids = [str(uuid4())], [str(uuid4())]
    db.commit()

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            assert not tools and "整理报告时使用" not in str(messages)
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=1)
            return ToolResponse("普通回答", [], self.last_usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    events, _ = send(admin, chat["id"], model["id"])
    assert events[-1].json()["status"] == "completed", events[-1].json().get("error_detail")
    assert events[-1].json()["tool_events"] == []


def test_retired_or_stale_skill_protocol_content_does_not_reenter_requests(monkeypatch):
    from types import SimpleNamespace
    from app import mcp_client
    from app.thinking import history_message
    config = SimpleNamespace(continuation_required=True, site_provider_id="provider", model="model",
                             thinking=SimpleNamespace(adapter="deepseek"), api_protocol="chat_completions", connection_id="connection")
    row = SimpleNamespace(role="assistant", content="Historical answer", encrypted_continuation="opaque")
    messages = [{"role": "assistant", "tool_calls": [{"id": "load", "function": {"name": "load_skill"}}]},
                {"role": "tool", "tool_call_id": "load", "content": json.dumps({"skill_id": "old-private", "instructions": "Private instructions"})}]
    private = {"provider": "provider", "model": "model", "adapter": "deepseek", "messages": messages}
    monkeypatch.setattr(mcp_client, "decrypt_headers", lambda _: {"continuation": json.dumps(private)})
    plain = [{"role": "assistant", "content": "Historical answer"}]
    assert history_message(row, config, []) == plain
    messages[-1]["content"] = json.dumps({"id": "platform", "version": 1, "instructions": "Approved method"})
    assert history_message(row, config, [("platform", 1)]) == messages
    assert history_message(row, config, [("platform", 2)]) == plain
    assert history_message(row, config, []) == plain
    messages[0]["tool_calls"][0]["function"]["name"] = "read_skill_resource"
    assert history_message(row, config, [("platform", 1)]) == messages
    assert history_message(row, config, [("platform", 2)]) == plain
    messages[0]["tool_calls"][0]["function"]["name"] = "mcp_old_tool"
    assert history_message(row, config, [("platform", 1)]) == plain
    messages[-1]["content"] = json.dumps({"content": "Private external result",
        "_extension": {"tool": "mcp_old_tool", "version": 3}})
    assert history_message(row, config, [("mcp_old_tool", 3)]) == messages
    assert history_message(row, config, [("mcp_old_tool", 4)]) == plain
    assert history_message(row, config, []) == plain
