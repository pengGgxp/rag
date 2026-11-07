import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx_sse import EventSource
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import main, conversations as chat
from app.db import Base
from app.llm import LLMProviderError, ToolCall, ToolResponse
from app.models import ChatMessage, Conversation, CreditAccount, DocumentStatus
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
                libraries = json.loads(messages[0]["content"].split("\n本会话知识库：")[1])
                return ToolResponse(content=None, tool_calls=[ToolCall(
                    id="search", name="search_knowledge_base",
                    arguments={
                        "query": "needle",
                        "knowledge_base_ids": [item["id"] for item in libraries],
                    },
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
    authenticate(http, db)
    from auth_helpers import configured_model
    from app.models import LEGACY_WORKSPACE_ID
    configured_model(db, LEGACY_WORKSPACE_ID, monkeypatch)
    try:
        yield http, db, captured
    finally:
        http.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        db.close()
        engine.dispose()


def create(http, libraries):
    response = http.post("/conversations", json={"knowledge_base_ids": [str(item.id) for item in libraries]})
    assert response.status_code == 201, response.text
    return response.json()


def turn(http, id, query="needle", **extra):
    payload = {"request_id": str(uuid4()), "provider_id": model_id(http),
        **({"query": query} if query is not None else {}), **extra}
    response = http.post(f"/conversations/{id}/messages", json=payload)
    assert response.status_code == 200, response.text
    events = [(event.event, event.json()) for event in EventSource(generation_response(response)).iter_sse()]
    return events, payload


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
    assert captured[2][1]["content"] == "needle"
    assert captured[2][2]["role"] == "assistant"
    assert captured[2][-1]["content"] == "需要多长时间？"
    assert [name for name, _ in events].index("tool_call") < [name for name, _ in events].index("tool_result")
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"] * 2
    assert [message["sequence"] for message in detail["messages"]] == [0, 1, 2, 3]
    assert detail["active_request_id"] is None
    assert detail["title"] == "needle"
    assert http.get("/conversations").json()["items"][0]["id"] == detail["id"]


def test_multi_library_scope_and_deleted_filtering(client):
    http, db, _ = client
    a, b, outside = [add_library(db) for _ in range(3)]
    first = add_document(db, a, "needle a")
    second = add_document(db, b, "needle b")
    add_document(db, outside, "needle outside")
    add_document(db, a, "needle deleted", status=DocumentStatus.DELETED)
    conversation = create(http, [a, b, a])
    assert len(conversation["knowledge_bases"]) == 2
    events, _ = turn(http, conversation["id"])
    answer = events[-1][1]
    assert {source["chunk_id"] for source in answer["citations"]} == {
        str(first.chunks[0].id), str(second.chunks[0].id),
    }
    assert {trace["knowledge_base_id"] for trace in answer["traces"]} == {str(a.id), str(b.id)}
    exported = http.get(f"/conversations/{conversation['id']}/export")
    assert "text/markdown" in exported.headers["content-type"]
    assert "测试回答" in exported.text
    assert str(first.chunks[0].id) in exported.text
    assert "needle outside" not in exported.text


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
        "knowledge_base_id": str(library.id)}]


@pytest.mark.parametrize("state", ["outside", "deleted", "inactive", "missing"])
def test_unavailable_mentions_are_rejected_before_message_or_billing_creation(client, state):
    http, db, _ = client
    library = add_library(db)
    document = add_document(db, add_library(db) if state == "outside" else library, "needle")
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


def test_empty_retrieval_keeps_agent_clarification_instead_of_fixed_refusal(client):
    http, db, captured = client
    library = add_library(db)
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"])
    assert events[-1][1]["refused"] is False
    assert events[-1][1]["content"] == "没有找到相关资料。你能补充一下资料名称或问题背景吗？"
    assert events[-1][1]["citations"] == []
    assert events[-1][1]["traces"]
    assert len(captured) == 2
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


def test_followup_does_not_reuse_deleted_document_as_evidence(client):
    http, db, captured = client
    library = add_library(db)
    document = add_document(db, library, "needle duration is 3 days")
    conversation = create(http, [library])
    turn(http, conversation["id"])
    assert http.delete(f"/documents/{document.id}").status_code == 200
    events, _ = turn(http, conversation["id"], "需要多久？")
    assert events[-1][1]["content"] == "没有找到相关资料。你能补充一下资料名称或问题背景吗？"
    assert events[-1][1]["citations"] == []
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
    assert completed[-1][1]["id"] == failed["id"]
    assert completed[-1][1]["status"] == "completed"
    messages = http.get(f"/conversations/{conversation['id']}").json()["messages"]
    assert len(messages) == 2
    assert messages[0]["document_mentions"][0]["id"] == str(document.id)
    invalid = http.post(f"/conversations/{conversation['id']}/messages", json={
        "request_id": str(uuid4()), "provider_id": model_id(http), "retry_message_id": failed["id"],
    })
    assert invalid.status_code == 409


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
    assert http.post("/conversations", json={"knowledge_base_ids": []}).status_code == 201
    assert http.post("/conversations", json={"knowledge_base_ids": [str(uuid4())]}).status_code == 404
    conversation = create(http, [add_library(db)])
    id = conversation["id"]
    assert http.patch(f"/conversations/{id}", json={"title": "  产品问题  ", "revision": conversation["revision"]}).json()["title"] == "产品问题"
    assert http.patch(f"/conversations/{id}", json={"title": "  "}).status_code == 422
    assert http.post(f"/conversations/{id}/messages", json={"request_id": str(uuid4()), "query": "  "}).status_code == 422
    assert http.get(f"/conversations/{id}").json()["messages"] == []
