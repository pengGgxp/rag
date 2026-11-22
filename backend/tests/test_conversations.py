import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx_sse import EventSource
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app import main, conversations as chat
from app.db import Base
from app.llm import LLMProviderError, ToolCall, ToolResponse
from app.models import (
    ChatMessage, Conversation, ConversationLibrary, UserCreditAccount, DocumentStatus,
    Document, KnowledgeBase, KnowledgeBaseStatus, QueryTrace, User,
)
from test_retrieval import add_document, add_library
from auth_helpers import authenticate, model_id, generation_response


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'conversation.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    captured = []

    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            assert tool_choice == "auto"
            captured.append(messages)
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="discover", name="discover_knowledge_bases", arguments={})])
            if messages[-1].get("name") == "discover_knowledge_bases":
                catalog = json.loads(messages[-1]["content"])
                libraries = catalog["knowledge_bases"]
                if not libraries:
                    return ToolResponse(content="我暂时没有可用的资料依据。", tool_calls=[])
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base",
                    arguments={"query": "needle", "knowledge_base_ids": [libraries[0]["id"]]},
                )])
            sources = json.loads(messages[-1]["content"])["evidence"]
            text = (
                "测试回答 [" + sources[0]["chunk_id"] + "]"
                if sources else "没有找到相关资料。你能补充一下资料名称或问题背景吗？"
            )
            return ToolResponse(content=text, tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: Provider())
    monkeypatch.setattr(main, "OpenAICompatibleEmbeddingProvider", lambda: SimpleNamespace(
        status=SimpleNamespace(configured=False, provider=None, model=None, dimension=1024),
    ))
    main.app.dependency_overrides[main.get_db] = lambda: db
    http = TestClient(main.app)
    user = authenticate(http, db)
    from auth_helpers import configured_model
    configured_model(db, user.id, monkeypatch)
    try:
        yield http, db, captured
    finally:
        http.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        db.close()
        engine.dispose()


def create(http, libraries=()):
    response = http.post("/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()


def turn(http, id, query="needle", **extra):
    payload = {"request_id": str(uuid4()), "provider_id": model_id(http),
        **({"query": query} if query is not None else {}), **extra}
    response = http.post(f"/conversations/{id}/messages", json=payload)
    assert response.status_code == 200, response.text
    events = [(event.event, event.json()) for event in EventSource(generation_response(response)).iter_sse()]
    return events, payload


def test_search_evidence_document_id_can_be_reused_for_scoped_retrieval(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle scientific methods")
    document.chunks[0].version_group_id = document.version_group_id
    db.commit()
    assert document.id != document.version_group_id

    class Provider:
        step = 0

        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                arguments = {"knowledge_base_ids": [str(library.id)], "query": "needle"}
            elif self.step == 2:
                evidence = json.loads(messages[-1]["content"])["evidence"]
                assert evidence[0]["document_id"] == str(document.id)
                arguments = {"knowledge_base_ids": [str(library.id)], "query": "needle",
                    "document_ids": [evidence[0]["document_id"], evidence[0]["version_id"]]}
            else:
                result = json.loads(messages[-1]["content"])
                assert result["result_count"] == 1 and "error" not in result
                return ToolResponse(content="verified", tool_calls=[])
            return ToolResponse(content=None, tool_calls=[ToolCall(
                id=f"search-{self.step}", name="search_knowledge_base", arguments=arguments)])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **kwargs: Provider())
    events, _ = turn(http, create(http)["id"])
    results = [data for name, data in events if name == "tool_result"]
    assert [item["status"] for item in results] == ["completed", "completed"]
    assert events[-1][1]["status"] == "completed"


@pytest.mark.parametrize("unavailable", ["inactive", "deleted", "other_library", "old_version"])
def test_scoped_search_rejects_unavailable_stable_ids_and_inactive_versions(client, monkeypatch, unavailable):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, add_library(db) if unavailable == "other_library" else library, "needle")
    if unavailable in ("inactive", "old_version"):
        document.is_active = False
    if unavailable == "deleted":
        document.status = DocumentStatus.DELETED
    db.commit()
    identifier = document.id if unavailable == "old_version" else document.version_group_id

    class Provider:
        step = 0

        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(content=None, tool_calls=[ToolCall(id="invalid",
                    name="search_knowledge_base", arguments={"query": "needle",
                        "knowledge_base_ids": [str(library.id)], "document_ids": [str(identifier)]})])
            assert "error" in json.loads(messages[-1]["content"])
            return ToolResponse(content="unavailable", tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **kwargs: Provider())
    events, _ = turn(http, create(http)["id"])
    result = next(data for name, data in events if name == "tool_result")
    assert result["status"] == "error"
    assert result["summary"] == "所选文档当前不可用或不属于所选知识库"
    assert db.scalars(select(QueryTrace)).all() == []


def test_persists_streamed_turn_and_followup_context(client):
    http, db, captured = client
    library = add_library(db)
    add_document(db, library, "needle duration is 3 days")
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"])
    assert [name for name, _ in events].count("delta") >= 1
    assert "".join(data["text"] for name, data in events if name == "delta") == events[-1][1]["content"]
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["citations"]
    second, _ = turn(http, conversation["id"], "需要多长时间？")
    assert second[-1][1]["status"] == "completed"
    assert captured[3][1]["content"] == "needle"
    assert captured[3][2]["role"] == "assistant"
    assert captured[3][-1]["content"] == "需要多长时间？"
    assert [name for name, _ in events].index("tool_call") < [name for name, _ in events].index("tool_result")
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"] * 2
    assert [message["sequence"] for message in detail["messages"]] == [0, 1, 2, 3]
    assert detail["active_request_id"] is None
    assert detail["title"] == "needle"
    assert http.get("/conversations").json()["items"][0]["id"] == detail["id"]


def test_corrected_tool_feedback_persists_and_resumes_without_turn_failure(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    add_document(db, library, "needle")

    class Provider:
        step = 0

        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [ToolCall("bad", "search_knowledge_base", {"query": "needle"})])
            if self.step == 2:
                assert json.loads(messages[-1]["content"])["error_code"] == "knowledge_arguments"
                return ToolResponse(None, [ToolCall("fixed", "search_knowledge_base", {
                    "query": "needle", "knowledge_base_ids": [str(library.id)]})])
            return ToolResponse("已核实资料。", [])

    provider = Provider()
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **kwargs: provider)
    identifier = create(http)["id"]
    events, payload = turn(http, identifier)
    answer = events[-1][1]
    assert answer["status"] == "completed"
    assert [item["outcome"] for item in answer["tool_events"]] == ["feedback", "success"]
    saved = http.get(f"/conversations/{identifier}").json()["messages"][-1]
    assert saved["tool_events"] == answer["tool_events"]
    assert [item["call_id"] for item in saved["display_blocks"] if item["type"] == "activity"] == [
        item["call_id"] for item in saved["tool_events"]]
    turn(http, identifier, **payload)
    assert provider.step == 3


def test_agent_searches_selected_authorized_library_and_excludes_deleted_documents(client):
    http, db, _ = client
    a, b, outside = [add_library(db) for _ in range(3)]
    add_document(db, a, "needle a")
    add_document(db, b, "needle b")
    add_document(db, outside, "needle outside")
    deleted = add_document(db, a, "needle deleted", status=DocumentStatus.DELETED)
    conversation = create(http, [a, b, a])
    assert conversation["knowledge_bases"] == []
    events, _ = turn(http, conversation["id"])
    answer = events[-1][1]
    assert len(answer["traces"]) == 1
    assert {source["knowledge_base_id"] for source in answer["citations"]} == {
        answer["traces"][0]["knowledge_base_id"],
    }
    assert str(deleted.chunks[0].id) not in {source["chunk_id"] for source in answer["citations"]}
    exported = http.get(f"/conversations/{conversation['id']}/export")
    assert "text/markdown" in exported.headers["content-type"]
    assert "测试回答" in exported.text
    assert "needle deleted" not in exported.text


def test_document_mentions_persist_by_id_without_forcing_retrieval_scope(client):
    http, db, captured = client
    library = add_library(db)
    selected = add_document(db, library, "needle selected")
    other = add_document(db, library, "needle other")
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"], "总结 @资料.pdf", document_ids=[str(selected.id)])
    assert events[-1][1]["status"] == "completed"
    # The test provider chooses an unrestricted search, even with a mention present.
    assert {row["chunk_id"] for row in events[-1][1]["citations"]} == {
        str(selected.chunks[0].id), str(other.chunks[0].id)}
    assert str(selected.id) in captured[0][0]["content"]
    saved = http.get(f"/conversations/{conversation['id']}").json()["messages"][0]
    assert saved["document_mentions"] == [{"id": str(selected.id), "filename": selected.filename,
        "knowledge_base_id": str(library.id), "knowledge_base_name": library.name}]


@pytest.mark.parametrize("change", ["revoke", "delete", "replace"])
def test_retrieval_receipt_rechecks_access_and_document_version(client, change):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle retained evidence")
    chat_id = create(http)["id"]
    events, submitted = turn(http, chat_id)
    answer = events[-1][1]
    assert answer["status"] == "completed"
    tool = next(item for item in answer["tool_events"] if item["name"] == "search_knowledge_base")
    path = f"/conversations/{chat_id}/requests/{submitted['request_id']}/tools/{tool['execution_id']}"

    receipt = http.get(path)
    assert receipt.status_code == 200
    assert "no-store" in receipt.headers["Cache-Control"]
    assert "needle retained evidence" in receipt.json()["text"]
    assert http.get(path.replace(submitted["request_id"], str(uuid4()))).status_code == 404

    if change == "revoke":
        other = User(name="Other account", email="receipt-other@example.com")
        db.add(other)
        db.flush()
        library.account_id = library.owner_id = other.id
    elif change == "delete":
        document.status = DocumentStatus.DELETED
    else:
        document.is_active = False
    db.commit()
    denied = http.get(path)
    assert denied.status_code in (403, 404)
    assert "needle retained evidence" not in denied.text


def test_revoked_library_keeps_legacy_history_but_not_new_catalog_or_retrieval(client):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle private historical evidence")
    conversation = create(http)
    db.add(ConversationLibrary(
        conversation_id=UUID(conversation["id"]), knowledge_base_id=library.id,
    ))
    db.commit()
    events, _ = turn(http, conversation["id"])
    answer = events[-1][1]
    assert answer["citations"][0]["text"] == "needle private historical evidence"

    other = User(email="outside@example.com", name="Other")
    db.add(other)
    db.flush()
    library.owner_id = library.account_id = other.id
    db.commit()

    listed = http.get("/conversations").json()["items"]
    assert any(item["id"] == conversation["id"] for item in listed)
    detail = http.get(f"/conversations/{conversation['id']}")
    assert detail.status_code == 200
    assert detail.json()["messages"][-1]["citations"][0]["text"] == "needle private historical evidence"
    assert "needle private historical evidence" in http.get(
        f"/conversations/{conversation['id']}/export").text

    catalog = http.get("/conversations/knowledge-catalog").json()
    assert catalog == {"knowledge_bases": [], "documents": [], "resources": []}
    assert http.get("/conversations/search", params={
        "q": "needle", "knowledge_base_id": str(library.id),
    }).status_code == 404

    events, _ = turn(http, conversation["id"], "查找被撤权的资料")
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["citations"] == []
    assert events[-1][1]["content"] == "我暂时没有可用的资料依据。"
    denied = http.post(f"/conversations/{conversation['id']}/messages", json={
        "provider_id": model_id(http), "request_id": str(uuid4()), "query": "总结",
        "document_ids": [str(document.id)],
    })
    assert denied.status_code == 409


@pytest.mark.parametrize("state", ["deleted", "inactive", "missing"])
def test_unavailable_mentions_are_rejected_before_message_or_billing_creation(client, state):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle")
    if state == "deleted":
        document.status = DocumentStatus.DELETED
    if state == "inactive":
        document.is_active = False
    db.commit()
    conversation = create(http, [library])
    response = http.post(f"/conversations/{conversation['id']}/messages", json={
        "provider_id": model_id(http), "request_id": str(uuid4()), "query": "needle",
        "document_ids": [str(uuid4() if state == "missing" else document.id)],
    })
    assert response.status_code == 409
    assert http.get(f"/conversations/{conversation['id']}").json()["messages"] == []


def test_document_mention_is_not_limited_by_conversation_knowledge_bases(client, monkeypatch):
    http, db, captured = client
    conversation_library = add_library(db)
    mentioned_library = add_library(db)
    document = add_document(db, mentioned_library, "needle")
    conversation = create(http, [conversation_library])

    class MentionProvider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            captured.append(messages)
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="discover", name="discover_knowledge_bases",
                    arguments={"query": mentioned_library.name})])
            if messages[-1].get("name") == "discover_knowledge_bases":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base", arguments={
                        "query": "needle",
                        "knowledge_base_ids": [str(mentioned_library.id)],
                        "document_ids": [str(document.id)],
                    })])
            evidence = json.loads(messages[-1]["content"])["evidence"]
            return ToolResponse(content=f"资料依据 [{evidence[0]['chunk_id']}]", tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: MentionProvider())
    events, _ = turn(http, conversation["id"], "needle", document_ids=[str(document.id)])

    message = http.get(f"/conversations/{conversation['id']}").json()["messages"][0]
    assert message["document_mentions"][0]["id"] == str(document.id)
    assert [item["chunk_id"] for item in events[-1][1]["citations"]] == [str(document.chunks[0].id)]
    assert [item["knowledge_base_id"] for item in events[-1][1]["traces"]] == [str(mentioned_library.id)]


def test_agent_discovery_filters_catalog_and_searches_only_selected_library(client, monkeypatch):
    http, db, captured = client
    selected_library, unrelated_library = add_library(db), add_library(db)
    selected_library.name = "needle reference"
    unrelated_library.name = "unrelated reference"
    selected_document = add_document(db, selected_library, "needle evidence")
    add_document(db, unrelated_library, "needle unrelated evidence")
    db.commit()

    class SelectiveProvider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            captured.append(messages)
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="discover", name="discover_knowledge_bases", arguments={"query": "needle"})])
            if messages[-1].get("name") == "discover_knowledge_bases":
                catalog = json.loads(messages[-1]["content"])
                assert [item["id"] for item in catalog["knowledge_bases"]] == [str(selected_library.id)]
                assert [item["id"] for item in catalog["documents"]] == [str(selected_document.id)]
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base",
                    arguments={"query": "needle", "knowledge_base_ids": [str(selected_library.id)]})])
            evidence = json.loads(messages[-1]["content"])["evidence"]
            assert {item["knowledge_base_id"] for item in evidence} == {str(selected_library.id)}
            return ToolResponse(content="找到资料。", tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: SelectiveProvider())
    conversation = create(http)
    events, _ = turn(http, conversation["id"], "needle")

    assert {item["knowledge_base_id"] for item in events[-1][1]["citations"]} == {str(selected_library.id)}
    assert all(item["knowledge_base_id"] != str(unrelated_library.id) for item in events[-1][1]["traces"])


def test_empty_catalog_match_allows_model_to_refine_without_silent_scope_change(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    library.name = "Willow support"
    document = add_document(db, library, "needle support evidence")
    db.commit()

    class RefiningProvider:
        step = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.step += 1
            if self.step == 1:
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="narrow", name="discover_knowledge_bases",
                    arguments={"query": "Willow delivery support hours"})])
            if self.step == 2:
                result = json.loads(messages[-1]["content"])
                assert result["knowledge_bases"] == result["documents"] == []
                assert "query=''" in result["search_hint"]
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="list", name="discover_knowledge_bases", arguments={"query": ""})])
            if self.step == 3:
                result = json.loads(messages[-1]["content"])
                assert [row["id"] for row in result["knowledge_bases"]] == [str(library.id)]
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base",
                    arguments={"query": "needle", "knowledge_base_ids": [str(library.id)]})])
            result = json.loads(messages[-1]["content"])
            assert {row["version_id"] for row in result["evidence"]} == {str(document.id)}
            return ToolResponse(content="Found support evidence.", tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: RefiningProvider())
    events, _ = turn(http, create(http)["id"], "Find support hours.")
    assert events[-1][1]["content"] == "Found support evidence."


def test_agent_discovers_and_searches_documents_without_mentions(client, monkeypatch):
    http, db, _ = client
    libraries = [add_library(db), add_library(db)]
    selected = [add_document(db, library, "needle selected") for library in libraries]
    for library in libraries:
        library.name = "needle references"
        add_document(db, library, "needle unselected")
    db.commit()
    selected_ids = {str(document.id) for document in selected}
    library_ids = [str(library.id) for library in libraries]
    chunk_ids = {str(document.chunks[0].id) for document in selected}

    class DiscoveryProvider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="discover", name="discover_knowledge_bases", arguments={"query": "needle"})])
            if messages[-1].get("name") == "discover_knowledge_bases":
                catalog = json.loads(messages[-1]["content"])
                documents = [row for row in catalog["documents"] if row["id"] in selected_ids]
                assert len(documents) == 2
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base", arguments={
                        "query": "needle", "knowledge_base_ids": library_ids,
                        "document_ids": [row["id"] for row in documents] + [documents[0]["id"]],
                    })])
            evidence = json.loads(messages[-1]["content"])["evidence"]
            assert {row["chunk_id"] for row in evidence} == chunk_ids
            return ToolResponse(content="资料依据 " + " ".join(
                f"[{row['chunk_id']}]" for row in evidence), tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: DiscoveryProvider())
    conversation = create(http)
    events, _ = turn(http, conversation["id"])
    answer = events[-1][1]
    assert answer["status"] == "completed"
    assert {row["chunk_id"] for row in answer["citations"]} == chunk_ids
    assert {row["version_id"] for row in answer["citations"]} == selected_ids
    assert {row["knowledge_base_id"] for row in answer["traces"]} == set(library_ids)
    history = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    assert history[0]["document_mentions"] == history[0]["knowledge_base_mentions"] == []
    assert {row["chunk_id"] for row in history[1]["citations"]} == chunk_ids


@pytest.mark.parametrize("change", [
    "ownership_changed", "deleted", "inactive", "processing", "archived", "moved",
    "missing", "foreign_document", "foreign_library",
])
def test_discovered_document_search_rechecks_current_scope(client, monkeypatch, change):
    http, db, _ = client
    library = add_library(db)
    library.name = "needle authorized"
    document = add_document(db, library, "needle authorized evidence")
    other_library = add_library(db)
    other = User(email="outside@example.com", name="Other")
    db.add(other); db.flush()
    foreign_library = KnowledgeBase(account_id=other.id, owner_id=other.id, name="needle foreign")
    db.add(foreign_library); db.flush()
    foreign_document = add_document(db, foreign_library, "needle forbidden evidence")
    owner = db.scalar(select(User).where(User.email == "owner@example.com"))
    library.owner_id = owner.id
    other_owner_id = other.id
    db.commit()
    library_id, document_id = library.id, document.id
    other_id = other_library.id
    foreign_library_id, foreign_document_id = foreign_library.id, foreign_document.id
    original_retrieve = main.retrieve_knowledge_base
    retrieval_calls = []

    def retrieve(*args, **kwargs):
        retrieval_calls.append(args[0])
        return original_retrieve(*args, **kwargs)

    class StaleDiscoveryProvider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            if messages[-1]["role"] == "user":
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="discover", name="discover_knowledge_bases", arguments={"query": "needle"})])
            if messages[-1].get("name") == "discover_knowledge_bases":
                catalog = json.loads(messages[-1]["content"])
                assert {row["id"] for row in catalog["documents"]} == {str(document_id)}
                with Session(db.get_bind()) as changed:
                    if change == "ownership_changed":
                        changed_library = changed.get(KnowledgeBase, library_id)
                        changed_library.owner_id = changed_library.account_id = other_owner_id
                    elif change == "deleted":
                        changed.get(Document, document_id).status = DocumentStatus.DELETED
                    elif change == "inactive":
                        changed.get(Document, document_id).is_active = False
                    elif change == "processing":
                        changed.get(Document, document_id).status = DocumentStatus.PROCESSING
                    elif change == "archived":
                        changed.get(KnowledgeBase, library_id).status = KnowledgeBaseStatus.ARCHIVED
                    elif change == "moved":
                        changed.get(Document, document_id).knowledge_base_id = other_id
                    changed.commit()
                requested_document = (uuid4() if change == "missing" else foreign_document_id
                    if change in ("foreign_document", "foreign_library") else document_id)
                requested_library = foreign_library_id if change == "foreign_library" else library_id
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base", arguments={
                        "query": "needle", "knowledge_base_ids": [str(requested_library)],
                        "document_ids": [str(requested_document)],
                    })])
            result = json.loads(messages[-1]["content"])
            assert "error" in result and "evidence" not in result
            return ToolResponse(content="资料当前不可用，请重新选择。", tool_calls=[])

    monkeypatch.setattr(main, "retrieve_knowledge_base", retrieve)
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: StaleDiscoveryProvider())
    conversation = create(http)
    events, _ = turn(http, conversation["id"])
    answer = events[-1][1]
    assert answer["status"] == "completed"
    assert answer["citations"] == answer["traces"] == retrieval_calls == []
    assert answer["tool_events"][-1]["status"] == "error"


def test_empty_retrieval_keeps_agent_clarification_instead_of_fixed_refusal(client):
    http, db, captured = client
    library = add_library(db)
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"])
    assert events[-1][1]["refused"] is False
    assert events[-1][1]["content"] == "没有找到相关资料。你能补充一下资料名称或问题背景吗？"
    assert events[-1][1]["citations"] == []
    assert events[-1][1]["traces"]
    assert len(captured) == 3
    assert captured[-1][-1]["role"] == "tool"
    assert "has_sufficient_evidence" not in json.loads(captured[-1][-1]["content"])
    assert http.get(f"/conversations/{conversation['id']}").json()["messages"][-1]["content"] == events[-1][1]["content"]


def test_agent_direct_answer_does_not_require_retrieval_evidence(client, monkeypatch):
    http, _, _ = client
    library = add_library(client[1])
    conversation = create(http, [library])

    class DirectAnswerProvider:
        def complete_with_tools(self, _messages, _tools, *, tool_choice):
            assert tool_choice == "auto"
            return ToolResponse(content="你好！有什么可以帮你？", tool_calls=[])

        async def stream(self, _messages):
            raise AssertionError("direct ReAct answers must not call the answer stream")
            yield ""

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: DirectAnswerProvider())
    events, _ = turn(http, conversation["id"], "你好")
    done = events[-1][1]
    assert done["status"] == "completed"
    assert done["refused"] is False
    assert done["content"] == "你好！有什么可以帮你？"
    assert done["citations"] == []
    assert [name for name, _ in events].count("tool_call") == 0


def test_followup_does_not_reuse_deleted_document_as_evidence(client, monkeypatch):
    http, db, captured = client
    library = add_library(db)
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    document = add_document(db, library, "needle duration is 3 days", store=store)
    conversation = create(http, [library])
    turn(http, conversation["id"])
    assert http.delete(f"/documents/{document.id}").status_code == 200
    request_count = len(captured)
    events, _ = turn(http, conversation["id"], "需要多久？")
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["citations"] == []
    assert "没有找到相关资料" in events[-1][1]["content"]
    assert len(captured) == request_count + 3
    assert json.loads(captured[-1][-1]["content"])["evidence"] == []


def test_failure_retry_and_duplicate_request_do_not_duplicate_user_turn(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, library, "needle")
    conversation = create(http, [library])

    class Failing:
        def complete_with_tools(self, _messages, _tools, *, tool_choice):
            raise LLMProviderError("upstream failed")

        def complete(self, _messages):
            raise AssertionError("must not fall back to fixed RAG")

    original = chat.OpenAICompatibleChatProvider
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: Failing())
    events, payload = turn(http, conversation["id"], document_ids=[str(document.id)])
    failed = events[-1][1]
    assert failed["status"] == "failed"
    assert failed["content"] == ""
    assert failed["error_detail"] == "upstream failed"
    assert http.get(f"/conversations/{conversation['id']}").json()["active_request_id"] is None
    repeated = http.post(f"/conversations/{conversation['id']}/messages", json=payload)
    assert repeated.status_code == 200
    assert len(http.get(f"/conversations/{conversation['id']}").json()["messages"]) == 2
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", original)
    completed, _ = turn(http, conversation["id"], None, retry_message_id=failed["id"])
    assert completed[-1][1]["id"] != failed["id"]
    assert completed[-1][1]["status"] == "completed"
    messages = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    assert len(messages) == 4
    assert messages[1]["id"] == failed["id"] and messages[1]["status"] == "failed"
    assert messages[0]["document_mentions"][0]["id"] == str(document.id)
    invalid = http.post(f"/conversations/{conversation['id']}/messages", json={
        "request_id": str(uuid4()), "provider_id": model_id(http), "retry_message_id": failed["id"],
    })
    assert invalid.status_code == 409


def test_failed_retry_keeps_history_but_drops_revoked_mentions(client, monkeypatch):
    http, db, captured = client
    library = add_library(db)
    document = add_document(db, library, "needle")
    conversation = create(http)

    class Failing:
        def complete_with_tools(self, _messages, _tools, *, tool_choice):
            raise LLMProviderError("upstream failed")

        def complete(self, _messages):
            raise AssertionError("must not fall back to fixed RAG")

    original = chat.OpenAICompatibleChatProvider
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: Failing())
    events, _ = turn(http, conversation["id"], document_ids=[str(document.id)])
    failed = events[-1][1]
    assert failed["status"] == "failed"

    other = User(email="outside@example.com", name="Other")
    db.add(other)
    db.flush()
    library.owner_id = library.account_id = other.id
    db.commit()

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", original)
    retried, _ = turn(http, conversation["id"], None, retry_message_id=failed["id"])
    assert retried[-1][1]["status"] == "completed"
    assert retried[-1][1]["citations"] == []
    assert retried[-1][1]["traces"] == []
    assert str(document.id) not in captured[0][0]["content"]
    history = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    assert history[0]["document_mentions"][0]["id"] == str(document.id)


def test_stop_conflict_delete_and_restart_recovery(client):
    http, db, _ = client
    library = add_library(db)
    conversation = create(http, [library])
    id = conversation["id"]
    row = db.get(Conversation, UUID(id))
    request_id = uuid4()
    row.active_request_id = request_id
    row.next_sequence = 2
    pending = ChatMessage(
        conversation_id=row.id, sequence=1, role="assistant", status="streaming",
        request_id=request_id, content="已保存片段",
    )
    db.add(pending)
    db.commit()
    blocked = http.post(f"/conversations/{id}/messages", json={"provider_id": model_id(http), "request_id": str(uuid4()), "query": "needle"})
    assert blocked.status_code == 409
    assert http.post(f"/conversations/{id}/stop", json={"request_id": str(uuid4())}).status_code == 404
    stopped = http.post(f"/conversations/{id}/stop", json={"request_id": str(request_id)})
    assert stopped.json()["status"] == "cancelled"
    assert stopped.json()["content"] == "已保存片段"
    db.refresh(row)
    db.refresh(pending)
    row.active_request_id = uuid4()
    pending.status = "streaming"
    db.commit()
    chat.recover_interrupted_conversations(db)
    db.refresh(pending)
    assert pending.status == "interrupted"
    assert http.get(f"/conversations/{id}").json()["active_request_id"] is None
    assert http.delete(f"/conversations/{id}").status_code == 204
    assert http.get(f"/conversations/{id}").status_code == 404
    assert http.get("/conversations").json()["items"] == []
    assert http.post(f"/conversations/{id}/messages", json={"provider_id": model_id(http), "request_id": str(uuid4()), "query": "needle"}).status_code == 404


def test_cancelled_answer_cannot_be_overwritten_by_a_late_completion(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    add_document(db, library, "needle")
    conversation = create(http, [library])

    class LateProvider:
        def complete_with_tools(self, _messages, _tools, *, tool_choice):
            with Session(db.get_bind()) as other:
                current = other.get(Conversation, UUID(conversation["id"]))
                message = other.scalar(select(ChatMessage).where(
                    ChatMessage.request_id == current.active_request_id,
                ))
                message.status = "cancelled"
                current.active_request_id = None
                other.commit()
            return ToolResponse(content="不应覆盖取消状态", tool_calls=[])

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: LateProvider())
    turn(http, conversation["id"])
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert detail["active_request_id"] is None
    assert detail["messages"][-1]["status"] == "cancelled"
    assert detail["messages"][-1]["content"] == ""


def test_rename_and_validation(client):
    http, db, _ = client
    assert http.post("/conversations", json={}).status_code == 201
    assert http.post("/conversations", json={"knowledge_base_ids": [str(uuid4())]}).status_code == 422
    conversation = create(http)
    id = conversation["id"]
    assert http.patch(f"/conversations/{id}", json={"title": "  产品问题  ", "revision": conversation["revision"]}).json()["title"] == "产品问题"
    assert http.patch(f"/conversations/{id}", json={"title": "  "}).status_code == 422
    assert http.post(f"/conversations/{id}/messages", json={"request_id": str(uuid4()), "query": "  "}).status_code == 422
    assert http.get(f"/conversations/{id}").json()["messages"] == []
