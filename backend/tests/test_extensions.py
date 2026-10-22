import base64
from contextlib import nullcontext
import io
import json
import stat
import zipfile
import zlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from httpx_sse import EventSource
from pydantic import SecretStr
from sqlalchemy import select

from app import auth, conversations, extensions, mcp_client, skill_files
from app.llm import ToolCall, ToolResponse
from app.models import AgentSkillFile, AuditLog, LoginSession, MCPServer
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
    with zipfile.ZipFile(io.BytesIO(owner.get(f"/agent/skills/{item['id']}/export").content)) as archive:
        assert archive.read("report-writer/SKILL.md").decode() == MARKDOWN
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
    "---\nname: okay\ndescription: fine\ncompatibility: 1\n---\n",
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
            assert [tool["function"]["name"] for tool in tools] == ["load_skill", "read_skill_file"]
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


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in entries:
            archive.writestr(path, data)
    return output.getvalue()


def upload_bundle(owner, entries):
    return owner.post("/agent/skills/import", files={
        "files": ("skill.zip", archive_bytes(entries), "application/zip"),
    })


def encoded(path, content):
    return {"path": path, "content_base64": base64.b64encode(content).decode()}


def test_skill_bundle_roundtrip_atomic_edit_binary_files_and_cascade_delete(users):
    owner, other, db, _ = users
    markdown = MARKDOWN.replace("---\n\n", "license: LICENSE.txt\nmetadata:\n  version: '1.0'\nallowed-tools: Read\n---\n\n")
    entries = [
        ("report-writer/SKILL.md", markdown.encode()),
        ("report-writer/references/nested/guide.md", b"Reference secret"),
        ("report-writer/scripts/helper.py", b"raise RuntimeError('never execute')\n"),
        ("report-writer/assets/logo.png", b"\x89PNG\r\n\x1a\n\x00binary"),
        ("report-writer/LICENSE.txt", b"License text"),
    ]
    response = upload_bundle(owner, entries)
    assert response.status_code == 201, response.text
    item = response.json()
    path = f"/agent/skills/{item['id']}"
    assert len(item["files"]) == 5
    assert "Reference secret" not in json.dumps(item)
    assert other.get(f"{path}/file", params={"path": "references/nested/guide.md"}).status_code == 404
    assert owner.get(f"{path}/file", params={"path": "assets/logo.png"}).json()["content"] is None
    assert owner.get(f"{path}/file", params={"path": "assets/logo.png", "download": "true"}).content == entries[3][1]
    assert owner.get(f"{path}/file", params={"path": "../secrets"}).status_code == 422
    exported = owner.get(f"{path}/export")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == dict(entries)
    assert owner.post("/agent/skills/import", files={"files": ("again.zip", exported.content)}).status_code == 201
    # Legacy updates omit files and must not erase resources.
    assert owner.put(path, json={"markdown": markdown, "enabled": False}).status_code == 200
    assert len(owner.get(path).json()["files"]) == 5
    # A failed replacement must leave entrypoint, state and resources untouched.
    assert owner.put(path, json={"markdown": MARKDOWN, "files": [encoded("../oops", b"bad")]}).status_code == 422
    assert owner.get(path).json()["markdown"] == markdown
    assert owner.get(path).json()["enabled"] is False
    assert len(owner.get(path).json()["files"]) == 5
    update = owner.put(path, json={
        "markdown": markdown,
        "files": [
            {"path": "assets/logo.png"},
            encoded("references/nested/guide.md", b"Updated text"),
            encoded("assets/empty.txt", b""),
        ],
    })
    assert update.status_code == 200, update.text
    assert owner.get(f"{path}/file", params={"path": "references/nested/guide.md"}).json()["content"] == "Updated text"
    assert owner.get(f"{path}/file", params={"path": "scripts/helper.py"}).status_code == 404
    identifier = UUID(item["id"])
    assert owner.delete(path).status_code == 204
    assert db.scalar(select(AgentSkillFile).where(AgentSkillFile.skill_id == identifier)) is None


def test_skill_directory_and_single_markdown_import(users):
    owner, _, _, _ = users
    response = owner.post("/agent/skills/import", files=[
        ("files", ("SKILL.md", MARKDOWN)),
        ("files", ("guide.md", "Directory reference")),
    ], data={"paths": ["report-writer/SKILL.md", "report-writer/references/guide.md"]})
    assert response.status_code == 201, response.text
    assert len(response.json()["files"]) == 2
    assert owner.post("/agent/skills/import", files={"files": ("SKILL.md", MARKDOWN)}).status_code == 201
    mismatch = upload_bundle(owner, [("wrong-name/SKILL.md", MARKDOWN.encode())])
    assert mismatch.status_code == 422


@pytest.mark.parametrize("entries", [
    [("../SKILL.md", MARKDOWN.encode())],
    [("/SKILL.md", MARKDOWN.encode())],
    [("SKILL.md", MARKDOWN.encode()), ("a\\b", b"bad")],
    [("SKILL.md", MARKDOWN.encode()), ("C:/x", b"bad")],
    [("SKILL.md", MARKDOWN.encode()), ("a/./b", b"bad")],
    [("SKILL.md", MARKDOWN.encode()), ("a", b"one"), ("A", b"two")],
    [("SKILL.md", MARKDOWN.encode()), ("a", b"one"), ("a", b"two")],
    [("SKILL.md", MARKDOWN.encode()), ("a", b"one"), ("a/b", b"two")],
    [("SKILL.md", MARKDOWN.encode()), ("a/SKILL.md", MARKDOWN.encode())],  # valid supporting nested skill
    [("missing.md", b"bad")],
])
def test_skill_archive_path_rules(users, entries):
    response = upload_bundle(users[0], entries)
    expected = 201 if any(path == "a/SKILL.md" for path, _ in entries) else 422
    assert response.status_code == expected, response.text


def test_skill_archive_rejects_links_corruption_size_and_count_limits(users, monkeypatch):
    owner, _, _, _ = users
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    assert upload_bundle(owner, [("SKILL.md", MARKDOWN.encode()), (info, b"/etc/passwd")]).status_code == 422
    assert owner.post("/agent/skills/import", files={"files": ("skill.zip", b"invalid")}).status_code == 422
    monkeypatch.setattr(skill_files, "MAX_FILE_BYTES", 250)
    assert upload_bundle(owner, [("SKILL.md", MARKDOWN.encode()), ("assets/x", b"x" * 251)]).status_code == 413
    monkeypatch.setattr(skill_files, "MAX_FILES", 2)
    assert upload_bundle(owner, [
        ("SKILL.md", MARKDOWN.encode()), ("a", b"a"), ("b", b"b"),
    ]).status_code == 413
    assert owner.get("/agent/skills").json() == []


def test_agent_reads_relevant_supporting_file_progressively(users, monkeypatch):
    owner, _, _, _ = users
    item = skill(owner, files=[
        encoded("references/guide.md", b"The special reference code is ORCHID-527.\nSecond line.\n"),
        encoded("references/irrelevant.md", b"DO NOT PRELOAD THIS FILE"),
    ])
    conversation = owner.post("/conversations", json={"skill_ids": [item["id"]]}).json()

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            assert "ORCHID-527" not in messages[0]["content"]
            assert "DO NOT PRELOAD" not in json.dumps(messages)
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="load", name="load_skill", arguments={"skill_id": item["id"]},
                )])
            result = json.loads(messages[-1]["content"])
            if messages[-1]["name"] == "load_skill":
                assert result["capabilities"]["execute_scripts"] is False
                assert {file["path"] for file in result["files"]} == {
                    "SKILL.md", "references/guide.md", "references/irrelevant.md",
                }
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="read", name="read_skill_file",
                    arguments={"skill_id": item["id"], "path": "references/guide.md", "line_count": 1},
                )])
            assert "ORCHID-527" in result["content"]
            assert result["next_line"] == 2
            return ToolResponse(content="The code is ORCHID-527.", tool_calls=[])

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    final = turn(owner, conversation["id"])[-1][1]
    assert final["content"] == "The code is ORCHID-527."
    assert len(final["tool_events"]) == 2
    assert all(event["kind"] == "skill" for event in final["tool_events"])
    assert "ORCHID" not in json.dumps(final["tool_events"])


def runtime_for(client, db, ids):
    session = db.get(LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
    return extensions.ExtensionRuntime(lambda: nullcontext(db), auth.get_access(session, db), ids, [])


def test_skill_file_tools_enforce_live_selection_ownership_workspace_and_revocation(users):
    owner, other, db, identity = users
    item = skill(owner, files=[encoded("references/private.md", b"PRIVATE REFERENCE")])
    arguments = {"skill_id": item["id"], "path": "references/private.md"}
    runtime = runtime_for(owner, db, [item["id"]])
    assert runtime.execute("read_skill_file", arguments)["content"] == "PRIVATE REFERENCE"
    assert "error" in runtime_for(owner, db, []).execute("read_skill_file", arguments)
    assert "error" in runtime_for(other, db, [item["id"]]).execute("read_skill_file", arguments)
    share(owner, "other@example.com")
    switch(other, identity["workspace_id"])
    assert "error" in runtime_for(other, db, [item["id"]]).execute("read_skill_file", arguments)
    for invalid in ("../private.md", "/SKILL.md", "references\\private.md"):
        assert "error" in runtime.execute("read_skill_file", {**arguments, "path": invalid})
    assert "error" in runtime.execute("read_skill_file", {**arguments, "line_count": 201})
    assert owner.put(f"/agent/skills/{item['id']}", json={"markdown": MARKDOWN, "enabled": False}).status_code == 200
    assert "error" in runtime.execute("read_skill_file", arguments)
    assert owner.delete(f"/agent/skills/{item['id']}").status_code == 204
    assert "error" in runtime.execute("read_skill_file", arguments)


def test_skill_file_pagination_binary_output_and_stop_are_honored(users):
    owner, _, db, _ = users
    content = "x" * 70000 + "\n" + "尾行\n"
    item = skill(owner, files=[encoded("long.txt", content.encode()), encoded("asset.bin", b"\x00\xff")])
    runtime = runtime_for(owner, db, [item["id"]])
    arguments = {"skill_id": item["id"], "path": "long.txt", "line_count": 1}
    actual = ""
    while True:
        result = runtime.execute("read_skill_file", arguments)
        assert len(result["content"]) <= 32000
        actual += result["content"]
        if result["next_line"] is None:
            break
        arguments.update(start_line=result["next_line"], start_character=result["next_character"])
    assert actual == content
    binary = runtime.execute("read_skill_file", {"skill_id": item["id"], "path": "asset.bin"})
    assert binary["content"] is None
    assert "error" not in binary
    def stopped():
        raise RuntimeError("user stopped")
    with pytest.raises(RuntimeError, match="user stopped"):
        runtime.execute("read_skill_file", arguments, check_active=stopped)


@pytest.mark.parametrize("extra", [
    "metadata:\n  author: 42\n", "compatibility: ''\n", "allowed-tools: [Read]\n",
])
def test_skill_optional_frontmatter_validation(users, extra):
    markdown = MARKDOWN.replace("---\n\n", extra + "---\n\n")
    assert users[0].post("/agent/skills", json={"markdown": markdown}).status_code == 422


def test_minimal_skill_and_optional_metadata_are_preserved(users):
    minimal = "---\nname: minimal\ndescription: A valid minimal skill\nlicense: Apache-2.0\nmetadata:\n  author: example\n---\n"
    response = users[0].post("/agent/skills", json={"markdown": minimal})
    assert response.status_code == 201
    assert response.json()["markdown"] == minimal


def test_corrupt_zip_compressed_stream_returns_validation_error(users, monkeypatch):
    content = archive_bytes([("SKILL.md", MARKDOWN.encode())])
    def broken_stream(*_args, **_kwargs):
        raise zlib.error("invalid compressed stream")
    monkeypatch.setattr(zipfile.ZipFile, "open", broken_stream)
    response = users[0].post("/agent/skills/import", files={"files": ("bad.zip", content)})
    assert response.status_code == 422
    assert users[0].get("/agent/skills").json() == []


def test_skill_bundle_total_size_limit_is_atomic(users, monkeypatch):
    monkeypatch.setattr(skill_files, "MAX_BUNDLE_BYTES", 250)
    response = upload_bundle(users[0], [
        ("SKILL.md", MARKDOWN.encode()), ("first.txt", b"x" * 100), ("second.txt", b"y" * 100),
    ])
    assert response.status_code == 413
    assert users[0].get("/agent/skills").json() == []


def test_unicode_skill_name_and_files_roundtrip(users):
    markdown = MARKDOWN.replace("report-writer", "数据分析")
    response = upload_bundle(users[0], [
        ("数据分析/SKILL.md", markdown.encode()), ("数据分析/references/参考.md", "参考资料".encode()),
    ])
    assert response.status_code == 201, response.text
    item = response.json()
    exported = users[0].get(f"/agent/skills/{item['id']}/export")
    assert "%E6%95%B0" in exported.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("数据分析/references/参考.md").decode() == "参考资料"
