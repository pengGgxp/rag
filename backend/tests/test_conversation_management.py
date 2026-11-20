from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import conversation_jobs, conversations
from app.models import (ChatMessage, Conversation, ConversationEvent, ConversationExecution,
    ConversationTitleTask, UserCreditAccount)
from test_conversations import client, create, turn
from test_retrieval import add_library
from auth_helpers import model_id


def test_pagination_pin_revision_and_activity(client):
    http, db, _ = client
    now = datetime.now(timezone.utc)
    for index in range(35):
        item = create(http, [])
        row = db.get(Conversation, UUID(item["id"]))
        row.updated_at = now - timedelta(minutes=index)
    db.commit()
    first = http.get("/conversations").json()
    assert len(first["items"]) == 30
    second = http.get("/conversations", params={"cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert not set(item["id"] for item in first["items"]) & set(item["id"] for item in second["items"])
    target = second["items"][-1]
    changed = http.patch(f"/conversations/{target['id']}", json={"revision": target["revision"], "pinned": True,
        "title": "手动标题"})
    assert changed.status_code == 200
    assert changed.json()["updated_at"] == target["updated_at"]
    assert changed.json()["title_source"] == "manual"
    assert http.get("/conversations").json()["items"][0]["id"] == target["id"]
    assert http.patch(f"/conversations/{target['id']}", json={"revision": target["revision"], "title": "旧草稿"}).status_code == 409
    assert http.get("/conversations", params={"cursor": "bad"}).status_code == 422


def test_literal_search_snippet_and_deleted_visibility(client):
    http, db, _ = client
    a, b = create(http, []), create(http, [])
    first = db.get(Conversation, UUID(a["id"]))
    first.title = "费用 10%_\\ 与预算"
    second = db.get(Conversation, UUID(b["id"]))
    second.title = "项目计划"
    db.add(ChatMessage(conversation_id=second.id, sequence=0, role="assistant", content="正文费用 10%_\\ 已经核对",
        display_blocks=[{"type": "thinking", "text": "绝不能被搜索"}]))
    db.commit()
    result = http.get("/conversations/search", params={"q": "10%_\\"}).json()
    assert [item["id"] for item in result["items"]] == [a["id"], b["id"]]
    assert result["items"][1]["message_id"] and "正文" in result["items"][1]["snippet"]
    assert http.get("/conversations/search", params={"q": "绝不能被搜索"}).json()["items"] == []
    assert http.get("/conversations/search", params={"q": "费用", "has_attachments": True}).json()["items"] == []
    http.delete(f"/conversations/{b['id']}")
    assert len(http.get("/conversations/search", params={"q": "费用"}).json()["items"]) == 1


def test_submission_is_durable_and_worker_is_idempotent(client):
    http, db, captured = client
    item = create(http, [add_library(db)])
    body = {"request_id": str(uuid4()), "provider_id": model_id(http), "query": "hello"}
    first = http.post(f"/conversations/{item['id']}/messages", json=body)
    assert first.json()["status"] == "queued" and not captured
    assert http.post(f"/conversations/{item['id']}/messages", json=body).json() == first.json()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    from app.main import retrieve_knowledge_base
    conversation_jobs.run(body["request_id"], factory, retrieve_knowledge_base)
    count = len(captured)
    conversation_jobs.run(body["request_id"], factory, retrieve_knowledge_base)
    assert len(captured) == count
    identifier = UUID(body["request_id"])
    with factory() as session:
        row = session.get(ConversationExecution, identifier)
        assert row.status == "completed"
        assert "test-only" not in row.encrypted_payload
        events = list(session.scalars(select(ConversationEvent).where(ConversationEvent.request_id == identifier)))
        assert len(events) > 1
    response = http.get(f"/conversations/{item['id']}/requests/{identifier}/events", params={"after": len(events) - 1})
    assert response.text.count("event: done") == 1 and "event: message" not in response.text


@pytest.mark.parametrize("name", ["generate_image", "edit_image", "start_preview"])
def test_background_tool_results_survive_reconnect_without_reexecution(client, monkeypatch, name):
    from app.agent import AgentRun
    http, db, _ = client
    item = create(http, [])
    identifier, linked = uuid4(), str(uuid4())
    calls = []
    result = {"call_id": "rich-tool", "name": name, "label": name, "kind": "tool", "status": "completed"}
    if name == "start_preview":
        result["preview"] = {"id": linked, "status": "ready", "terminal": False,
            "name": "算法可视化", "summary": "可预览", "url": None}
    else:
        result["image_task"] = {"task_id": linked, "status": "running", "terminal": False,
            "summary": "正在生成图片", "files": [], "credits": "0", "cancel_requested": False}

    def fake_agent(**kwargs):
        calls.append(name)
        yield "tool_result", result
        yield "delta", {"block_id": "text", "text": "任务已提交"}
        yield "complete", AgentRun([], [], [], [], 1, "任务已提交", [])

    monkeypatch.setattr(conversations, "stream_retrieval_agent", fake_agent)
    assert http.post(f"/conversations/{item['id']}/messages", json={"request_id": str(identifier),
        "provider_id": model_id(http), "query": "处理图片并制作算法页面"}).status_code == 200
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    # Execute with no browser subscription, then reload and reconnect repeatedly.
    conversation_jobs.run(identifier, factory)
    detail = http.get(f"/conversations/{item['id']}").json()
    assert detail["messages"][-1]["status"] == "completed"
    assert detail["messages"][-1]["tool_events"] == [result]
    for _ in range(2):
        events = http.get(f"/conversations/{item['id']}/requests/{identifier}/events")
        assert linked in events.text and "event: done" in events.text
        conversation_jobs.run(identifier, factory)
    assert calls == [name]


def test_api_restart_preserves_worker_owned_generation(client):
    http, db, _ = client
    item = create(http, [])
    identifier = uuid4()
    http.post(f"/conversations/{item['id']}/messages", json={"request_id": str(identifier), "provider_id": model_id(http), "query": "hello"})
    conversations.recover_interrupted_conversations(db)
    db.expire_all()
    assert db.get(ConversationExecution, identifier).status == "queued"
    assert db.get(Conversation, UUID(item["id"])).active_request_id == identifier
    assert db.scalar(select(ChatMessage).where(ChatMessage.request_id == identifier)).status == "streaming"


def test_stale_worker_is_not_replayed_and_releases_slot(client, monkeypatch):
    http, db, _ = client
    item = create(http, [])
    identifier = uuid4()
    http.post(f"/conversations/{item['id']}/messages", json={"request_id": str(identifier), "provider_id": model_id(http), "query": "hello"})
    row = db.get(ConversationExecution, identifier)
    row.status, row.heartbeat_at = "running", datetime.now(timezone.utc) - timedelta(minutes=4)
    db.commit()
    dispatched = []
    monkeypatch.setattr(conversation_jobs, "dispatch", dispatched.append)
    conversation_jobs.recover(sessionmaker(bind=db.get_bind()))
    db.expire_all()
    assert not dispatched
    assert db.get(ConversationExecution, identifier).status == "interrupted"
    assert db.get(Conversation, UUID(item["id"])).active_request_id is None


def test_platform_title_usage_and_manual_rename_race(client, monkeypatch):
    http, db, _ = client
    item = create(http, [add_library(db)])
    _, body = turn(http, item["id"], "请帮我整理季度项目计划")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    before = db.scalar(select(func.sum(UserCreditAccount.balance)))
    from app import llm
    from app.llm import LLMUsage
    class TitleProvider:
        last_usage = LLMUsage(prompt_tokens=20, completion_tokens=8)
        def __init__(self, config):
            pass
        def complete(self, prompt):
            assert len(prompt) == 2 and "tools" not in str(prompt)
            return "季度项目计划整理"
    monkeypatch.setattr(llm, "OpenAICompatibleChatProvider", TitleProvider)
    conversation_jobs.title_run(item["id"], factory)
    db.expire_all()
    title = db.get(ConversationTitleTask, UUID(item["id"]))
    assert title.status == "completed" and title.usage["prompt_tokens"] == 20
    assert title.cost_reference["payer"] == "platform"
    assert db.get(Conversation, UUID(item["id"])).title == "季度项目计划整理"
    assert db.scalar(select(func.sum(UserCreditAccount.balance))) == before
    conversation_jobs.title_run(item["id"], factory)
    assert db.scalar(select(func.sum(UserCreditAccount.balance))) == before


def test_title_failure_is_not_retried_and_manual_edit_wins(client, monkeypatch):
    http, db, _ = client
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    from app import llm
    calls = []
    class RacingProvider:
        last_usage = None
        def __init__(self, config):
            pass
        def complete(self, prompt):
            calls.append(prompt)
            with factory() as session:
                conversation = session.get(Conversation, identifier)
                conversation.title, conversation.title_source = "用户最终标题", "manual"
                conversation.revision += 1
                session.commit()
            return "自动生成标题不应覆盖"
    monkeypatch.setattr(llm, "OpenAICompatibleChatProvider", RacingProvider)
    item = create(http, [add_library(db)])
    turn(http, item["id"])
    identifier = UUID(item["id"])
    conversation_jobs.title_run(identifier, factory)
    db.expire_all()
    assert db.get(Conversation, identifier).title == "用户最终标题"
    conversation_jobs.title_run(identifier, factory)
    assert len(calls) == 1

    failed = create(http, [add_library(db)])
    turn(http, failed["id"])
    class FailingProvider(RacingProvider):
        def complete(self, prompt):
            calls.append(prompt)
            raise RuntimeError("mock unavailable")
    monkeypatch.setattr(llm, "OpenAICompatibleChatProvider", FailingProvider)
    before = db.scalar(select(func.sum(UserCreditAccount.balance)))
    conversation_jobs.title_run(failed["id"], factory)
    conversation_jobs.title_run(failed["id"], factory)
    db.expire_all()
    assert len(calls) == 2
    assert db.get(ConversationTitleTask, UUID(failed["id"])).status == "failed"
    assert db.get(Conversation, UUID(failed["id"])).title_source == "temporary"
    assert db.scalar(select(func.sum(UserCreditAccount.balance))) == before

    invalid = create(http, [add_library(db)])
    turn(http, invalid["id"])
    before = db.scalar(select(func.sum(UserCreditAccount.balance)))
    class InvalidProvider:
        def __init__(self, config):
            raise RuntimeError("configuration unavailable")
    monkeypatch.setattr(llm, "OpenAICompatibleChatProvider", InvalidProvider)
    conversation_jobs.title_run(invalid["id"], factory)
    db.expire_all()
    assert db.get(ConversationTitleTask, UUID(invalid["id"])).status == "failed"
    assert db.scalar(select(func.sum(UserCreditAccount.balance))) == before


def test_search_and_request_access_are_user_scoped_not_workspace_scoped(client):
    http, db, _ = client
    from app.models import User
    item = create(http, [])
    identifier = uuid4()
    http.post(f"/conversations/{item['id']}/messages", json={"request_id": str(identifier),
        "provider_id": model_id(http), "query": "隔离关键词"})
    other = User(email="other-chat@example.com", name="Other")
    db.add(other); db.flush()
    conversation = db.get(Conversation, UUID(item["id"]))
    original_owner = conversation.owner_id
    conversation.owner_id = other.id
    db.commit()
    assert http.get("/conversations/search", params={"q": "隔离关键词"}).json()["items"] == []
    assert not http.get("/conversations").json()["items"]
    assert http.get(f"/conversations/{item['id']}/requests/{identifier}").status_code == 404
    assert http.get(f"/conversations/{item['id']}/requests/{identifier}/events").status_code == 404
    conversation.owner_id, conversation.account_id = original_owner, other.id
    db.commit()
    assert len(http.get("/conversations/search", params={"q": "隔离关键词"}).json()["items"]) == 1
    assert len(http.get("/conversations").json()["items"]) == 1
    assert http.get(f"/conversations/{item['id']}/requests/{identifier}").status_code == 200


def test_deleted_queued_answer_never_calls_model_and_settles(client):
    http, db, captured = client
    item = create(http, [])
    identifier = uuid4()
    http.post(f"/conversations/{item['id']}/messages", json={"request_id": str(identifier),
        "provider_id": model_id(http), "query": "不要运行"})
    assert http.delete(f"/conversations/{item['id']}").status_code == 204
    conversation_jobs.run(identifier, sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    db.expire_all()
    assert not captured
    assert db.get(ConversationExecution, identifier).status != "running"
    assert db.get(Conversation, UUID(item["id"])).active_request_id is None
    assert http.get(f"/conversations/{item['id']}/requests/{identifier}/events").status_code == 404


def test_first_successful_answer_after_failure_gets_one_title(client):
    http, db, _ = client
    library = add_library(db)
    item = create(http, [library])
    _, first = turn(http, item["id"])
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    identifier = UUID(item["id"])
    db.query(ConversationTitleTask).filter(ConversationTitleTask.conversation_id == identifier).delete()
    message = db.scalar(select(ChatMessage).where(ChatMessage.request_id == UUID(first["request_id"])))
    message.status = "failed"
    db.commit()
    _, second = turn(http, item["id"], "请继续整理")
    db.expire_all()
    task = db.get(ConversationTitleTask, identifier)
    assert task and task.request_id == UUID(second["request_id"])
    # Losing dispatch after completion is recoverable without rerunning an answer.
    db.delete(task); db.commit()
    conversation_jobs.recover(factory)
    db.expire_all()
    assert db.get(ConversationTitleTask, identifier).request_id == UUID(second["request_id"])


def test_original_request_stays_idempotent_after_retry_replaces_message_request(client):
    http, db, captured = client
    item = create(http, [add_library(db)])
    _, original = turn(http, item["id"])
    message = db.scalar(select(ChatMessage).where(ChatMessage.request_id == UUID(original["request_id"])))
    message.status = "failed"
    db.commit()
    _, retry = turn(http, item["id"], query=None, retry_message_id=str(message.id))
    count = len(captured)
    response = http.post(f"/conversations/{item['id']}/messages", json=original)
    assert response.status_code == 200 and response.json()["request_id"] == original["request_id"]
    assert len(captured) == count
    assert db.scalar(select(func.count()).select_from(ConversationExecution).where(
        ConversationExecution.conversation_id == UUID(item["id"]))) == 2


def test_search_cursor_filters_and_legacy_role_does_not_revoke_owner(client):
    http, db, _ = client
    library = add_library(db)
    now = datetime.now(timezone.utc)
    identifiers = []
    for index in range(24):
        item = create(http, [library] if index < 4 else [])
        identifiers.append(item["id"])
        row = db.get(Conversation, UUID(item["id"]))
        row.title, row.updated_at = f"检索测试 {index}", now - timedelta(days=index)
        if index < 4:
            db.add(ChatMessage(conversation_id=row.id, sequence=0, role="user",
                content="检索测试消息", knowledge_base_mentions=[{"id": str(library.id), "name": library.name}]))
    db.commit()
    first = http.get("/conversations/search", params={"q": "检索测试"}).json()
    second = http.get("/conversations/search", params={"q": "检索测试", "cursor": first["next_cursor"]}).json()
    assert len(first["items"]) == 20 and len(second["items"]) == 4
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    filtered = http.get("/conversations/search", params={"q": "检索测试", "knowledge_base_id": str(library.id),
        "since": (now-timedelta(days=2, hours=1)).isoformat(), "until": (now-timedelta(hours=1)).isoformat()}).json()
    assert {row["id"] for row in filtered["items"]} == set(identifiers[1:3])
    owner = db.get(Conversation, UUID(identifiers[0])).owner_id
    library.owner_id = library.account_id = owner
    db.commit()
    response = http.get("/conversations/search", params={
        "q": "检索测试", "knowledge_base_id": str(library.id),
    })
    assert response.status_code == 200 and len(response.json()["items"]) == 4
    assert http.get(f"/conversations/{identifiers[0]}").status_code == 200
