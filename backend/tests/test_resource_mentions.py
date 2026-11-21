import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import auth, conversations as chat, workspace_files
from app.llm import LLMProviderError, ToolCall, ToolResponse
from app.models import (ChatAttachment, ChatMessage, CloudComputerCommand, CloudComputerSession,
    CreditLedgerEntry, ModelUsageRequest, User, WorkspaceFile, WorkspaceFileVersion)
from auth_helpers import model_id
from test_conversations import client, create, turn
from test_resources import access_for, resource_store, upload
from test_retrieval import add_document, add_library


def provider(monkeypatch, calls, *, fail=False, read=False):
    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            calls.append(json.loads(json.dumps(messages)))
            if fail:
                raise LLMProviderError("local upstream failed")
            if read and messages[-1]["role"] == "user" and "用户本轮提及的资源文件（" in messages[0]["content"]:
                references, _ = json.JSONDecoder().raw_decode(messages[0]["content"].split(
                    "用户本轮提及的资源文件（仅定位元数据，按任务需要调用文件工具）：", 1)[1])
                assert "read_file" in {tool["function"]["name"] for tool in tools}
                return ToolResponse(content=None, tool_calls=[ToolCall(id="read", name="read_file",
                    arguments={"id": references[0]["id"],
                        "computer_id": "00000000-0000-4000-a000-000000000112"})])
            return ToolResponse(content="已收到。", tool_calls=[])
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_: Provider())


def submit(http, conversation, identifiers):
    return http.post(f"/conversations/{conversation['id']}/messages", json={
        "provider_id": model_id(http), "request_id": str(uuid4()), "query": "处理所选文件",
        "resource_ids": identifiers})


def test_catalog_groups_filters_and_workspace_isolation(client, resource_store):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle")
    document.filename = "facts.txt"
    db.commit()
    item = upload(http)
    actor = access_for(http, db)
    other = workspace_files._save_bytes(actor.user_id, actor, db, "reports/facts.txt",
        b"second", "text/plain")
    literal = workspace_files._save_bytes(actor.user_id, actor, db, "100%_facts.txt",
        b"literal", "text/plain")
    outside = User(name="Outside", email="outside-resource@example.com")
    db.add(outside); db.flush()
    outside_actor = auth.Access(outside.id, "")
    workspace_files._save_bytes(outside.id, outside_actor, db, "facts.txt", b"outside", "text/plain")
    result = http.get("/conversations/knowledge-catalog?q=facts.txt").json()
    assert result["documents"][0]["id"] == str(document.id)
    assert {row["id"] for row in result["resources"]} == {item["id"], other["id"], literal["id"]}
    assert len({row["path"] for row in result["resources"]}) == 3
    assert http.get("/conversations/knowledge-catalog", params={"q": "%_"}).json()["resources"] == [
        next(row for row in result["resources"] if row["id"] == literal["id"])]
    assert resource_store[1] == []


@pytest.mark.parametrize("cloud_enabled", [False, True])
def test_reference_is_metadata_only_and_does_not_start_cloud(client, resource_store, monkeypatch, cloud_enabled):
    http, db, _ = client
    item = upload(http, body=b"SECRET FILE BODY")
    attachment_ids = set(db.scalars(select(ChatAttachment.id)))
    if cloud_enabled:
        from app import shared_resources
        from test_cloud_computer import enable_execution
        original_backend = shared_resources.backend
        fake, _ = enable_execution(db, access_for(http, db), monkeypatch)
        monkeypatch.setattr(shared_resources, "backend", original_backend)
    calls = []
    provider(monkeypatch, calls)
    conversation = create(http)
    events, _ = turn(http, conversation["id"], "确认收到", resource_ids=[item["id"], item["id"]])
    assert events[-1][1]["status"] == "completed"
    messages = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    saved = messages[0]["resource_mentions"]
    assert saved == [{"id": item["id"], "filename": item["path"].split("/")[-1], "path": item["path"],
        "content_type": item["content_type"], "size_bytes": len(b"SECRET FILE BODY")}]
    assert saved[0]["id"] in calls[0][0]["content"]
    assert "SECRET FILE BODY" not in json.dumps(calls)
    assert not messages[0]["attachments"] and not events[-1][1]["citations"]
    assert set(db.scalars(select(ChatAttachment.id))) == attachment_ids
    assert not db.scalars(select(CloudComputerSession)).all()
    assert not db.scalars(select(CloudComputerCommand)).all()
    assert not any("cloud" in entry.kind for entry in db.scalars(select(CreditLedgerEntry)))
    assert resource_store[1] == []
    if cloud_enabled:
        assert fake.creates == fake.starts == 0
    calls.clear()
    turn(http, conversation["id"], "下一轮")
    assert item["id"] not in calls[0][0]["content"]


def test_model_without_tools_rejects_resource_before_billing(client, resource_store):
    http, db, _ = client
    item = upload(http)
    from app.models import SiteModelProvider
    shared = db.scalar(select(SiteModelProvider))
    shared.thinking_config = {"kind": "unsupported", "supports_tools": False}
    db.commit()
    assert submit(http, create(http), [item["id"]]).status_code == 422
    assert not db.scalars(select(ChatMessage)).all()
    assert not db.scalars(select(ModelUsageRequest)).all()


def test_retry_uses_current_metadata_and_preserves_history(client, resource_store, monkeypatch):
    http, db, _ = client
    item = upload(http)
    calls = []
    provider(monkeypatch, calls, fail=True)
    conversation = create(http)
    events, _ = turn(http, conversation["id"], resource_ids=[item["id"]])
    failed = events[-1][1]
    from app.shared_resources import object_key
    actor = access_for(http, db)
    resource_store[0][object_key(actor.user_id, item["path"])] = b"new current bytes"
    calls.clear(); provider(monkeypatch, calls)
    events, _ = turn(http, conversation["id"], None, retry_message_id=failed["id"])
    assert events[-1][1]["status"] == "completed"
    current, _ = json.JSONDecoder().raw_decode(calls[0][0]["content"].split(
        "用户本轮提及的资源文件（仅定位元数据，按任务需要调用文件工具）：", 1)[1])
    assert current[0]["size_bytes"] == len(b"new current bytes")
    saved = http.get(f"/conversations/{conversation['id']}").json()["messages"][0]["resource_mentions"]
    assert saved[0]["size_bytes"] == len(b"resource facts")
    assert resource_store[1] == []


@pytest.mark.parametrize("state", ["deleted", "missing", "unavailable", "version_deleted", "outside", "unknown"])
def test_invalid_reference_rejected_before_messages_and_billing(client, resource_store, state):
    http, db, _ = client
    item = upload(http)
    row = db.get(WorkspaceFile, UUID(item["id"]))
    version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id))
    if state == "deleted": row.deleted_at = auth.now()
    if state == "missing": resource_store[0].clear()
    if state == "unavailable": version.available = False
    if state == "version_deleted": version.deleted_at = auth.now()
    if state == "outside":
        outside = User(name="Outside", email="outside-resource@example.com")
        db.add(outside)
        db.flush()
        row.owner_user_id = outside.id
    db.commit()
    conversation = create(http)
    before = len(db.scalars(select(CreditLedgerEntry)).all())
    response = submit(http, conversation, [str(uuid4()) if state == "unknown" else item["id"]])
    assert response.status_code == 409, response.text
    assert not db.scalars(select(ChatMessage)).all()
    assert not db.scalars(select(ModelUsageRequest)).all()
    assert len(db.scalars(select(CreditLedgerEntry)).all()) == before


@pytest.mark.parametrize("change", ["deleted", "revoked"])
def test_worker_revalidates_after_submission(client, resource_store, monkeypatch, change):
    http, db, _ = client
    item = upload(http)
    calls = []
    provider(monkeypatch, calls)
    response = submit(http, create(http), [item["id"]])
    assert response.status_code == 200
    if change == "deleted":
        resource_store[0].clear()
    else:
        db.get(User, access_for(http, db).user_id).is_suspended = True
        db.commit()
    from app.conversation_jobs import run
    from app import main
    from sqlalchemy.orm import sessionmaker
    run(response.json()["request_id"], factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        retrieve=main.retrieve_knowledge_base)
    if change == "revoked":
        assert not calls
    else:
        assert item["id"] not in calls[0][0]["content"]
    assert resource_store[1] == []


def test_retry_keeps_historical_reference_but_skips_missing_file(client, resource_store, monkeypatch):
    http, db, _ = client
    item = upload(http)
    calls = []
    provider(monkeypatch, calls, fail=True)
    conversation = create(http)
    events, _ = turn(http, conversation["id"], resource_ids=[item["id"]])
    failed = events[-1][1]
    assert failed["status"] == "failed"
    resource_store[0].clear()
    calls.clear(); provider(monkeypatch, calls)
    events, _ = turn(http, conversation["id"], None, retry_message_id=failed["id"])
    assert events[-1][1]["status"] == "completed"
    assert item["id"] not in calls[0][0]["content"]
    messages = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    assert len(messages) == 4
    assert messages[1]["id"] == failed["id"] and messages[1]["status"] == "failed"
    assert messages[0]["resource_mentions"][0]["id"] == item["id"]
    assert messages[2]["resource_mentions"] == messages[0]["resource_mentions"]


def test_combined_mention_limit(client, resource_store):
    http, db, _ = client
    item = upload(http)
    library = add_library(db)
    documents = [add_document(db, library, "needle") for _ in range(8)]
    response = http.post(f"/conversations/{create(http)['id']}/messages", json={
        "provider_id": model_id(http), "request_id": str(uuid4()), "query": "总结",
        "document_ids": [str(row.id) for row in documents], "resource_ids": [item["id"]]})
    assert response.status_code == 422
    assert not db.scalars(select(ChatMessage)).all()


def test_agent_reads_reference_through_existing_file_tool(client, resource_store, monkeypatch):
    http, db, _ = client
    item = upload(http)
    from test_cloud_computer import enable_execution
    enable_execution(db, access_for(http, db), monkeypatch)
    from test_cloud_computer_mount import MemoryMount
    storage = MemoryMount(); storage.objects = resource_store[0]
    monkeypatch.setattr("app.shared_resources.backend", lambda _: storage)
    operations = []
    def execute(db, access, conversation_id, request_id, arguments, *_, **execution):
        assert execution["agent_tool_execution_id"] and callable(execution["on_progress"])
        access.revalidate(db)
        operations.append(arguments["file_operation"])
        return {"status": "completed", "computer_id": arguments["computer_id"],
            "cloud_computer": {}, "command_id": str(uuid4()),
            "file_result": {"value": {"content": "resource facts"}}}
    monkeypatch.setattr("app.cloud_computer.execute", execute)
    calls = []
    provider(monkeypatch, calls, read=True)
    events, _ = turn(http, create(http)["id"], resource_ids=[item["id"]])
    assert events[-1][1]["status"] == "completed"
    assert operations[0]["name"] == "read_file" and operations[0]["path"] == item["path"]
    assert operations[0]["reference"]["id"] == item["id"]
    assert "resource facts" in calls[-1][-1]["content"]
    assert events[-1][1]["citations"] == []
