import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx_sse import EventSource
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main, conversations as chat
from app.db import Base
from app.llm import LLMProviderError
from app.models import ChatMessage, Conversation, DocumentStatus
from test_retrieval import add_document, add_library
from auth_helpers import authenticate


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    captured = []

    class Provider:
        def complete(self, messages):
            captured.append(("rewrite", messages))
            return "needle duration"

        async def stream(self, messages):
            captured.append(("answer", messages))
            sources = json.loads(messages[-1]["content"])["sources"]
            yield "测试回答"
            yield f" [{sources[0]['chunk_id']}]"

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", Provider)
    monkeypatch.setattr(main, "OpenAICompatibleEmbeddingProvider", lambda: SimpleNamespace(
        status=SimpleNamespace(configured=False, provider=None, model=None, dimension=1024),
    ))
    main.app.dependency_overrides[main.get_db] = lambda: db
    http = TestClient(main.app)
    authenticate(http, db)
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
    payload = {"request_id": str(uuid4()), **({"query": query} if query is not None else {}), **extra}
    response = http.post(f"/conversations/{id}/messages", json=payload)
    assert response.status_code == 200, response.text
    events = [(event.event, event.json()) for event in EventSource(response).iter_sse()]
    return events, payload


def test_persists_streamed_turn_and_followup_context(client):
    http, db, captured = client
    library = add_library(db)
    add_document(db, library, "needle duration is 3 days")
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"])
    assert [name for name, _ in events].count("delta") == 2
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["citations"]
    second, _ = turn(http, conversation["id"], "需要多长时间？")
    assert second[-1][1]["status"] == "completed"
    rewrite = next(messages for kind, messages in captured if kind == "rewrite")
    assert json.loads(rewrite[-1]["content"])["history"][0]["content"] == "needle"
    generation = [messages for kind, messages in captured if kind == "answer"][-1]
    assert len(json.loads(generation[-1]["content"])["history"]) == 2
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"] * 2
    assert [message["sequence"] for message in detail["messages"]] == [0, 1, 2, 3]
    assert detail["active_request_id"] is None
    assert detail["title"] == "needle"
    assert http.get("/conversations").json()[0]["id"] == detail["id"]


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


def test_no_evidence_refusal_is_saved_without_calling_answer_model(client):
    http, db, captured = client
    library = add_library(db)
    conversation = create(http, [library])
    events, _ = turn(http, conversation["id"])
    assert events[-1][1]["refused"] is True
    assert events[-1][1]["citations"] == []
    assert events[-1][1]["traces"]
    assert captured == []
    assert http.get(f"/conversations/{conversation['id']}").json()["messages"][-1]["refused"] is True


def test_followup_does_not_reuse_deleted_document_as_evidence(client):
    http, db, captured = client
    library = add_library(db)
    document = add_document(db, library, "needle duration is 3 days")
    conversation = create(http, [library])
    turn(http, conversation["id"])
    assert http.delete(f"/documents/{document.id}").status_code == 200
    events, _ = turn(http, conversation["id"], "需要多久？")
    assert events[-1][1]["refused"] is True
    assert events[-1][1]["citations"] == []
    assert len([kind for kind, _ in captured if kind == "answer"]) == 1


def test_failure_retry_and_duplicate_request_do_not_duplicate_user_turn(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    add_document(db, library, "needle")
    conversation = create(http, [library])

    class Failing:
        async def stream(self, _messages):
            yield "部分回答"
            raise LLMProviderError("upstream failed")

    original = chat.OpenAICompatibleChatProvider
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", Failing)
    events, payload = turn(http, conversation["id"])
    failed = events[-1][1]
    assert failed["status"] == "failed"
    assert failed["content"] == "部分回答"
    assert http.get(f"/conversations/{conversation['id']}").json()["active_request_id"] is None
    repeated = http.post(f"/conversations/{conversation['id']}/messages", json=payload)
    assert repeated.status_code == 200
    assert len(http.get(f"/conversations/{conversation['id']}").json()["messages"]) == 2
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", original)
    completed, _ = turn(http, conversation["id"], None, retry_message_id=failed["id"])
    assert completed[-1][1]["id"] == failed["id"]
    assert completed[-1][1]["status"] == "completed"
    assert len(http.get(f"/conversations/{conversation['id']}").json()["messages"]) == 2
    invalid = http.post(f"/conversations/{conversation['id']}/messages", json={
        "request_id": str(uuid4()), "retry_message_id": failed["id"],
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
    blocked = http.post(f"/conversations/{id}/messages", json={"request_id": str(uuid4()), "query": "needle"})
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
    assert http.get("/conversations").json() == []
    assert http.post(f"/conversations/{id}/messages", json={"request_id": str(uuid4()), "query": "needle"}).status_code == 404


def test_cancelled_answer_cannot_be_overwritten_by_a_late_completion(client, monkeypatch):
    http, db, _ = client
    library = add_library(db)
    add_document(db, library, "needle")
    conversation = create(http, [library])

    class LateProvider:
        async def stream(self, _messages):
            yield "已保存片段"
            with Session(db.get_bind()) as other:
                current = other.get(Conversation, UUID(conversation["id"]))
                message = other.scalar(select(ChatMessage).where(
                    ChatMessage.request_id == current.active_request_id,
                ))
                message.status = "cancelled"
                current.active_request_id = None
                other.commit()
            yield "不应覆盖取消状态"

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", LateProvider)
    turn(http, conversation["id"])
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert detail["active_request_id"] is None
    assert detail["messages"][-1]["status"] == "cancelled"
    assert detail["messages"][-1]["content"] == "已保存片段"


def test_rename_and_validation(client):
    http, db, _ = client
    assert http.post("/conversations", json={"knowledge_base_ids": []}).status_code == 422
    assert http.post("/conversations", json={"knowledge_base_ids": [str(uuid4())]}).status_code == 404
    conversation = create(http, [add_library(db)])
    id = conversation["id"]
    assert http.patch(f"/conversations/{id}", json={"title": "  产品问题  "}).json()["title"] == "产品问题"
    assert http.patch(f"/conversations/{id}", json={"title": "  "}).status_code == 422
    assert http.post(f"/conversations/{id}/messages", json={"request_id": str(uuid4()), "query": "  "}).status_code == 422
    assert http.get(f"/conversations/{id}").json()["messages"] == []
