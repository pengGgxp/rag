import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from httpx_sse import EventSource
from pydantic import SecretStr
from sqlalchemy import select

from app import conversations, extensions, mcp_client
from app.llm import ToolCall, ToolResponse
from app.models import AuditLog, MCPServer
from test_auth import register, share, switch

MARKDOWN = "---\nname: report-writer\ndescription: Write a structured report\n---\n\nUse headings: Findings, Evidence, Next steps.\n"
TOOL = {
    "name": "calculate",
    "description": "Calculate the sum.",
    "inputSchema": {
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"], "additionalProperties": False,
    },
}


@pytest.fixture
def users(accounts, monkeypatch):
    (owner, other, _), db = accounts
    identity = register(owner)
    register(other, "other@example.com", "Other")
    settings = mcp_client.get_settings().model_copy(update={
        "mcp_credential_key": SecretStr(Fernet.generate_key().decode()),
    })
    monkeypatch.setattr(mcp_client, "get_settings", lambda: settings)
    return owner, other, db, identity


def skill(owner, **changes):
    response = owner.post("/agent/skills", json={"markdown": MARKDOWN, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def server(owner, **changes):
    response = owner.post("/agent/mcp", json={
        "name": "Calculator", "url": "https://mcp.example.com/mcp", **changes,
    })
    assert response.status_code == 201, response.text
    return response.json()


def authorize(owner, item, monkeypatch):
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [TOOL])
    response = owner.post(f"/agent/mcp/{item['id']}/sync")
    assert response.status_code == 200, response.text
    updated = owner.put(f"/agent/mcp/{item['id']}", json={
        "name": item["name"], "url": item["url"], "allowed_tools": ["calculate"],
        "acknowledge_external_access": True,
    })
    assert updated.status_code == 200, updated.text
    return updated.json()


def turn(owner, identifier, query="Do the task"):
    response = owner.post(f"/conversations/{identifier}/messages", json={
        "query": query, "request_id": str(uuid4()),
    })
    assert response.status_code == 200, response.text
    return [(event.event, event.json()) for event in EventSource(response).iter_sse()]


def test_skills_crud_import_export_and_user_workspace_isolation(users):
    owner, other, db, identity = users
    item = skill(owner)
    assert "markdown" not in owner.get("/agent/skills").json()[0]
    assert owner.get(f"/agent/skills/{item['id']}").json()["markdown"] == MARKDOWN
    assert owner.get(f"/agent/skills/{item['id']}/export").text == MARKDOWN
    assert other.get("/agent/skills").json() == []
    assert other.get(f"/agent/skills/{item['id']}").status_code == 404
    assert other.put(f"/agent/skills/{item['id']}", json={"markdown": MARKDOWN}).status_code == 404
    assert other.delete(f"/agent/skills/{item['id']}").status_code == 404
    share(owner, "other@example.com")
    switch(other, identity["workspace_id"])
    assert other.get("/agent/skills").json() == []
    assert other.get(f"/agent/skills/{item['id']}/export").status_code == 404
    assert other.post("/conversations", json={"skill_ids": [item["id"]]}).status_code == 404
    updated = owner.put(f"/agent/skills/{item['id']}", json={"markdown": MARKDOWN, "enabled": False})
    assert updated.status_code == 200
    assert owner.post("/conversations", json={"skill_ids": [item["id"]]}).status_code == 409
    assert owner.delete(f"/agent/skills/{item['id']}").status_code == 204
    assert owner.get("/agent/skills").json() == []
    assert "Use headings" not in str(db.scalars(select(AuditLog)).all())


@pytest.mark.parametrize("markdown", [
    "# No frontmatter", "---\nname: Wrong Name\ndescription: abc\n---\nBody",
    "---\nname: okay\ndescription: 1\n---\nBody",
    "---\nname: okay\ndescription: fine\n---\n",
    "---\nname: !!python/object/apply:os.system ['echo unsafe']\n---\nBody",
])
def test_skill_metadata_is_validated_without_executing_yaml(users, markdown):
    assert users[0].post("/agent/skills", json={"markdown": markdown}).status_code == 422


def test_mcp_credentials_are_encrypted_never_returned_and_can_be_preserved_or_cleared(users):
    owner, other, db, _ = users
    item = server(owner, headers={"Authorization": "Bearer top-secret"})
    assert item["header_names"] == ["Authorization"]
    assert "top-secret" not in json.dumps(item)
    stored = db.get(MCPServer, UUID(item["id"]))
    assert "top-secret" not in stored.encrypted_headers
    assert mcp_client.decrypt_headers(stored.encrypted_headers) == {"Authorization": "Bearer top-secret"}
    update = {"name": "Renamed", "url": item["url"]}
    assert owner.put(f"/agent/mcp/{item['id']}", json=update).status_code == 200
    db.refresh(stored)
    assert mcp_client.decrypt_headers(stored.encrypted_headers)["Authorization"] == "Bearer top-secret"
    assert "top-secret" not in owner.get("/agent/mcp").text
    logs = db.scalars(select(AuditLog)).all()
    assert all("top-secret" not in json.dumps(log.metadata_json) for log in logs)
    assert other.get("/agent/mcp").json() == []
    assert other.post(f"/agent/mcp/{item['id']}/sync").status_code == 404
    assert other.delete(f"/agent/mcp/{item['id']}").status_code == 404
    assert owner.put(f"/agent/mcp/{item['id']}", json={**update, "headers": {}}).json()["has_credentials"] is False


def test_mcp_requires_consent_and_resync_revokes_changed_tool_authorization(users, monkeypatch):
    owner, _, _, _ = users
    item = server(owner)
    assert item["tools"] == item["allowed_tools"] == []
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [TOOL])
    assert owner.post(f"/agent/mcp/{item['id']}/sync").status_code == 200
    payload = {"name": item["name"], "url": item["url"], "allowed_tools": ["calculate"]}
    assert owner.put(f"/agent/mcp/{item['id']}", json=payload).status_code == 422
    assert owner.put(f"/agent/mcp/{item['id']}", json={
        **payload, "acknowledge_external_access": True,
    }).status_code == 200
    assert owner.post(f"/agent/mcp/{item['id']}/sync").json()["allowed_tools"] == ["calculate"]
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [{**TOOL, "description": "Changed"}])
    assert owner.post(f"/agent/mcp/{item['id']}/sync").json()["allowed_tools"] == []
    assert owner.put(f"/agent/mcp/{item['id']}", json={
        **payload, "url": "https://different.example.com/mcp", "acknowledge_external_access": True,
    }).status_code == 422


def test_agent_loads_selected_skill_progressively_and_persists_tool_summary(users, monkeypatch):
    owner, _, _, _ = users
    item = skill(owner)
    conversation = owner.post("/conversations", json={"skill_ids": [item["id"]]}).json()

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            assert [tool["function"]["name"] for tool in tools] == ["load_skill"]
            if messages[-1]["role"] == "user":
                assert "Use headings:" not in messages[0]["content"]
                assert "Write a structured report" in messages[0]["content"]
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="skill", name="load_skill", arguments={"skill_id": item["id"]},
                )])
            assert "Use headings:" in json.loads(messages[-1]["content"])["instructions"]
            return ToolResponse(content="Findings\nEvidence\nNext steps", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    events = turn(owner, conversation["id"])
    final = events[-1][1]
    assert final["status"] == "completed"
    assert final["citations"] == []
    assert final["tool_events"][0]["kind"] == "skill"
    assert final["tool_events"][0]["status"] == "completed"
    assert owner.get(f"/conversations/{conversation['id']}").json()["messages"][-1]["tool_events"] == final["tool_events"]
    assert "instructions" not in json.dumps(final["tool_events"])


def test_agent_calls_only_selected_and_authorized_mcp_tools(users, monkeypatch):
    owner, _, _, _ = users
    item = authorize(owner, server(owner), monkeypatch)
    calls = []
    monkeypatch.setattr(mcp_client, "call", lambda *args: calls.append(args) or {
        "content": [{"type": "text", "text": "7"}], "isError": False,
    })
    conversation = owner.post("/conversations", json={"mcp_server_ids": [item["id"]]}).json()

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            assert len(tools) == 1
            alias = tools[0]["function"]["name"]
            assert alias.startswith("mcp_")
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="calc", name=alias, arguments={"a": 3, "b": 4},
                )])
            assert json.loads(messages[-1]["content"])["content"][0]["text"] == "7"
            return ToolResponse(content="7", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    events = turn(owner, conversation["id"])
    final = events[-1][1]
    assert final["content"] == "7"
    assert final["tool_events"][0]["label"] == "Calculator · calculate"
    assert final["citations"] == []
    assert len(calls) == 1


def test_revoked_mcp_is_not_executed_even_after_model_selected_it(users, monkeypatch):
    owner, _, db, _ = users
    item = authorize(owner, server(owner), monkeypatch)
    monkeypatch.setattr(mcp_client, "call", lambda *_: pytest.fail("revoked MCP must not execute"))
    conversation = owner.post("/conversations", json={"mcp_server_ids": [item["id"]]}).json()

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            if messages[-1]["role"] == "user":
                row = db.get(MCPServer, UUID(item["id"]))
                row.enabled = False
                db.commit()
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="revoked", name=tools[0]["function"]["name"], arguments={"a": 3, "b": 4},
                )])
            assert "error" in json.loads(messages[-1]["content"])
            return ToolResponse(content="The connection was disabled.", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    final = turn(owner, conversation["id"])[-1][1]
    assert final["status"] == "completed"
    assert final["tool_events"][0]["status"] == "error"


def test_conversation_extension_selection_is_mutable_only_when_idle(users):
    owner, other, db, _ = users
    item = skill(owner)
    conversation = owner.post("/conversations", json={}).json()
    path = f"/conversations/{conversation['id']}/extensions"
    assert owner.put(path, json={"skill_ids": [item["id"]]}).json()["skill_ids"] == [item["id"]]
    assert other.put(path, json={}).status_code == 404
    from app.models import Conversation
    row = db.get(Conversation, UUID(conversation["id"]))
    row.active_request_id = uuid4()
    db.commit()
    assert owner.put(path, json={}).status_code == 409


def test_authorization_revoked_during_mcp_connection_blocks_actual_execution(users, monkeypatch):
    owner, _, db, _ = users
    item = authorize(owner, server(owner), monkeypatch)
    conversation = owner.post("/conversations", json={"mcp_server_ids": [item["id"]]}).json()

    def connecting(_url, _transport, _headers, _tool, _arguments, before_call):
        row = db.get(MCPServer, UUID(item["id"]))
        row.allowed_tools = []
        db.commit()
        before_call()
        pytest.fail("revoked tools must not be sent to the remote service")

    monkeypatch.setattr(mcp_client, "call", connecting)

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="connect", name=tools[0]["function"]["name"], arguments={"a": 1, "b": 2},
                )])
            assert "授权已变化" in json.loads(messages[-1]["content"])["error"]
            return ToolResponse(content="The tool was not executed.", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    final = turn(owner, conversation["id"])[-1][1]
    assert final["status"] == "completed"
    assert final["tool_events"][0]["status"] == "error"


@pytest.mark.parametrize("broken_reference", [False, True])
def test_mcp_invalid_arguments_and_unselected_tool_are_not_executed(users, monkeypatch, broken_reference):
    owner, _, db, _ = users
    item = authorize(owner, server(owner), monkeypatch)
    if broken_reference:
        row = db.get(MCPServer, UUID(item["id"]))
        row.tools = [{**TOOL, "inputSchema": {"$ref": "#/$defs/missing"}}]
        db.commit()
    monkeypatch.setattr(mcp_client, "call", lambda *_: pytest.fail("invalid tool call must not execute"))
    conversation = owner.post("/conversations", json={"mcp_server_ids": [item["id"]]}).json()

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[
                    ToolCall(id="invalid", name=tools[0]["function"]["name"], arguments={"a": "oops"}),
                    ToolCall(id="outside", name="mcp_unselected", arguments={}),
                ])
            assert all("error" in json.loads(message["content"]) for message in messages[-2:])
            return ToolResponse(content="No tools were executed.", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    final = turn(owner, conversation["id"])[-1][1]
    assert final["status"] == "completed"
    assert all(event["status"] == "error" for event in final["tool_events"])


def test_mcp_rejects_remote_output_schema_without_saving_catalog(users, monkeypatch):
    owner, _, _, _ = users
    item = server(owner)
    monkeypatch.setattr(mcp_client, "discover", lambda *_: [{
        **TOOL, "outputSchema": {"$ref": "http://169.254.169.254/secret"},
    }])
    response = owner.post(f"/agent/mcp/{item['id']}/sync")
    assert response.status_code == 502
    assert owner.get("/agent/mcp").json()[0]["tools"] == []
