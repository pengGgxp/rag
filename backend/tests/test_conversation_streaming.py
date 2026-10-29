"""Exercise actual HTTP streams, not a prebuffered MockTransport response."""
import json
import socket
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from uuid import UUID, uuid4
from dataclasses import replace

import httpx
import pytest
from httpx_sse import EventSource
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import conversations as chat, main
from app.llm import ChatProviderConfig, LLMProviderError, LLMUsage, OpenAICompatibleChatProvider, ToolResponse
from app.models import BillingSnapshot, ChatMessage, ContextCheckpoint, Conversation, CreditAccount, SiteModelProvider
from auth_helpers import model_id
from test_conversations import client, create, turn


@contextmanager
def upstream(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            respond(self, payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def sse(handler, data):
    handler.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
    handler.wfile.flush()


def test_real_stream_delivers_text_before_upstream_finishes_and_assembles_tools():
    release, ended = Event(), Event()
    captured = []

    def respond(handler, payload):
        captured.append(payload)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "先查资料", "reasoning_content": "PRIVATE-REASONING"}}]})
        assert release.wait(5), "consumer did not receive early text"
        handler.wfile.write(b"id: upstream-heartbeat\n: keepalive\n\n")
        handler.wfile.flush()
        sse(handler, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
            "function": {"name": "search_", "arguments": '{"query":'}}]}}]})
        sse(handler, {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"name": "knowledge_base", "arguments": '"needle"}'}}]}, "finish_reason": "tool_calls"}]})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 60}}})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 0}})
        ended.set()

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "test", max_output_tokens=100))
        stream = provider.stream_with_tools([{"role": "user", "content": "test"}], [])
        name, data = next(stream)
        assert (name, data) == ("delta", {"text": "先查资料"})
        assert not ended.is_set()
        release.set()
        events = list(stream)
    result = events[-1][1]
    assert result.tool_calls[0].name == "search_knowledge_base"
    assert result.tool_calls[0].arguments == {"query": "needle"}
    assert result.usage.prompt_tokens == 100 and result.usage.cached_input_tokens == 60
    assert result.protocol_fields is None
    assert "PRIVATE" not in repr(events)
    assert captured[0]["stream"] is True and captured[0]["max_tokens"] == 100


def test_truncated_stream_keeps_confirmed_usage_but_does_not_invent_success():
    def respond(handler, _payload):
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "部分正文"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "test"))
        stream = provider.stream_with_tools([], [])
        assert next(stream)[1]["text"] == "部分正文"
        with pytest.raises(LLMProviderError):
            list(stream)
        assert provider.last_usage.prompt_tokens == 7


def test_length_finish_still_reads_the_final_usage_frame():
    def respond(handler, _payload):
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "部分正文"}, "finish_reason": "length"}]})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 9}})
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "test"))
        stream = provider.stream_with_tools([], [])
        assert next(stream)[1]["text"] == "部分正文"
        with pytest.raises(LLMProviderError, match="未完整生成"):
            list(stream)
        assert provider.last_usage.prompt_tokens == 17 and provider.last_usage.completion_tokens == 9


def test_json_fallback_is_one_delta_with_diagnostic():
    def respond(handler, _payload):
        handler.send_response(200); handler.send_header("Content-Type", "application/json"); handler.end_headers()
        handler.wfile.write(json.dumps({"choices": [{"message": {"content": "one response"}}]}).encode())
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "test"))
        events = list(provider.stream_with_tools([], []))
    assert [name for name, _ in events] == ["delta", "complete"]
    assert provider.stream_diagnostic == "upstream_returned_json"


@pytest.mark.parametrize("disconnect", [False, True])
def test_api_sse_reaches_client_before_actual_upstream_closes(client, monkeypatch, disconnect):
    import uvicorn
    http, db, _ = client
    conversation = create(http, [])
    identifier = model_id(http)
    release, ended = Event(), Event()
    stop_signals = []
    bridge = chat.stream_events
    def observed_events(execution, stopped):
        stop_signals.append(stopped)
        return bridge(execution, stopped)
    monkeypatch.setattr(chat, "stream_events", observed_events)
    def respond(handler, _payload):
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "首段正文"}}]})
        assert release.wait(8)
        sse(handler, {"choices": [{"delta": {"content": "后续正文"}, "finish_reason": "stop"}]})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 4}})
        ended.set()
    factory = sessionmaker(bind=db.get_bind())
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[main.get_db] = database
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, lifespan="off", log_level="error"))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    with upstream(respond) as url:
        monkeypatch.setattr(chat, "OpenAICompatibleChatProvider",
            lambda config: OpenAICompatibleChatProvider(config=replace(config, base_url=url, site_provider_id=None)))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            with httpx.stream("POST", f"http://127.0.0.1:{listener.getsockname()[1]}/conversations/{conversation['id']}/messages",
                headers={**dict(http.headers), "Cookie": f"rag_session={http.cookies.get('rag_session')}"},
                json={"request_id": str(uuid4()), "provider_id": identifier, "query": "test"}, timeout=10) as response:
                assert response.status_code == 200
                stream = EventSource(response).iter_sse()
                first = next(event for event in stream if event.event == "delta")
                assert first.json()["text"] == "首段正文" and not ended.is_set()
                if not disconnect:
                    release.set()
                    remaining = list(stream)
                    assert remaining[-1].event == "done" and remaining[-1].json()["status"] == "completed"
            if disconnect:
                assert stop_signals and stop_signals[0].wait(5)
                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with factory() as session:
                        saved = session.scalar(select(ChatMessage).where(
                            ChatMessage.conversation_id == UUID(conversation["id"]), ChatMessage.role == "assistant"))
                        if saved.status != "streaming":
                            break
                    time.sleep(.02)
                assert saved.status == "cancelled" and saved.content == "首段正文"
                with factory() as session:
                    snapshots = session.scalars(select(BillingSnapshot).where(
                        BillingSnapshot.request_id == saved.request_id)).all()
                    assert sum(row.context.get("call_usage", {}).get("prompt_tokens") or 0 for row in snapshots) == 8
        finally:
            release.set()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            main.app.dependency_overrides[main.get_db] = lambda: db


def test_each_tool_is_interleaved_persisted_and_has_a_unique_public_id(client, monkeypatch):
    http, db, _ = client
    captured = []

    class Provider:
        last_usage = None
        def complete_with_tools(self, *_args, **_kwargs):
            raise AssertionError("must stream")
        def stream_with_tools(self, messages, tools, **_kwargs):
            from app.llm import ToolCall
            captured.append(messages)
            round = len(captured)
            text = ["先查第一处。", "再核对第二处。", "核对完成。"][round - 1]
            yield "delta", {"text": text[:2]}
            yield "delta", {"text": text[2:]}
            self.last_usage = LLMUsage(prompt_tokens=100, completion_tokens=10)
            yield "complete", ToolResponse(content=text, tool_calls=[
                ToolCall("reused-upstream-id", "unknown", {})] if round < 3 else [], usage=self.last_usage)

    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_: Provider())
    conversation = create(http, [])
    events, payload = turn(http, conversation["id"])
    done = events[-1][1]
    assert done["status"] == "completed"
    assert [block["type"] for block in done["display_blocks"]] == ["text", "activity", "text", "activity", "text"]
    assert len({item["call_id"] for item in done["tool_events"]}) == 2
    assert "arguments" not in json.dumps(done["tool_events"])
    assert done["content"] == "先查第一处。\n\n再核对第二处。\n\n核对完成。"
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert detail["messages"][-1]["display_blocks"] == done["display_blocks"]
    calls = db.scalars(select(BillingSnapshot).where(BillingSnapshot.request_id == UUID(payload["request_id"]))).all()
    assert len([row for row in calls if row.context.get("call_usage")]) == 3
    sequences = [data["event_sequence"] for _, data in events]
    assert sequences == list(range(1, len(sequences) + 1))


def context_fixture(client, *, length=1000):
    http, db, _ = client
    identifier = model_id(http)
    provider = db.get(SiteModelProvider, UUID(identifier))
    provider.context_capacity, provider.max_output_tokens = 16000, 2000
    conversation = create(http, [])
    for sequence in range(8):
        db.add(ChatMessage(conversation_id=UUID(conversation["id"]), sequence=sequence,
            role="user" if sequence % 2 == 0 else "assistant",
            content=f"record-{sequence} " + "详细历史数据。" * length, status="completed"))
    db.get(Conversation, UUID(conversation["id"])).next_sequence = 8
    db.commit()
    return http, db, conversation["id"], identifier


def summary_provider(monkeypatch, *, fail=False, text="目标：测试\n约束：保留事实\n决定：核对\n关键事实：42\n已执行操作：无\n待办：继续"):
    calls = []
    class Provider:
        last_usage = None
        def stream_with_tools(self, messages, tools, **kwargs):
            calls.append(messages)
            if fail:
                raise LLMProviderError("controlled failure")
            self.last_usage = LLMUsage(prompt_tokens=100, completion_tokens=20)
            yield "complete", ToolResponse(content=text, tool_calls=[], usage=self.last_usage)
        def complete_with_tools(self, *_args, **_kwargs):
            raise AssertionError("must stream")
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_: Provider())
    return calls


def compact(http, conversation, provider, request_id=None):
    response = http.post(f"/conversations/{conversation}/compact", json={"request_id": request_id or str(uuid4()),
        "provider_id": provider})
    assert response.status_code == 200, response.text
    return [(event.event, event.json()) for event in EventSource(response).iter_sse()]


def test_manual_compaction_preserves_recent_turns_and_repeat_is_free(client, monkeypatch):
    http, db, conversation, provider = context_fixture(client, length=60)
    calls = summary_provider(monkeypatch)
    before = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    request_id = str(uuid4())
    events = compact(http, conversation, provider, request_id)
    assert events[-1][1]["status"] == "completed"
    assert len(calls) == 1
    assert "record-3" in calls[0][1]["content"] and "record-4" not in calls[0][1]["content"]
    after = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    assert after["estimated_input_tokens"] < before["estimated_input_tokens"]
    checkpoints = db.scalars(select(ContextCheckpoint)).all()
    assert len(checkpoints) == 1 and checkpoints[0].through_sequence == 3
    balance = http.get("/usage").json()["balance"]
    assert compact(http, conversation, provider)[-1][1]["no_op"]
    assert compact(http, conversation, provider, request_id)[-1][1]["status"] == "completed"
    assert len(calls) == 1 and http.get("/usage").json()["balance"] == balance
    detail = http.get(f"/conversations/{conversation}").json()
    assert len(detail["messages"]) == 8 and "record-0" in detail["messages"][0]["content"]
    assert not detail["active_request_id"]


def test_failed_compaction_preserves_original_and_releases_hold(client, monkeypatch):
    http, db, conversation, provider = context_fixture(client, length=60)
    calls = summary_provider(monkeypatch, fail=True)
    before = http.get("/usage").json()["balance"]
    events = compact(http, conversation, provider)
    assert events[-1][1]["status"] == "failed"
    assert len(calls) == 1
    assert db.scalars(select(ContextCheckpoint)).all() == []
    assert http.get("/usage").json()["balance"] == before
    assert not http.get(f"/conversations/{conversation}").json()["active_request_id"]


def test_checkpoint_invalidated_when_original_changes(client, monkeypatch):
    http, db, conversation, provider = context_fixture(client, length=60)
    summary_provider(monkeypatch)
    compact(http, conversation, provider)
    compressed = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    row = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == UUID(conversation), ChatMessage.sequence == 0))
    row.content += "changed " * 100; db.commit()
    current = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    assert current["estimated_input_tokens"] > compressed["estimated_input_tokens"]


def test_missing_capacity_is_honest_and_no_history_compaction_is_free(client, monkeypatch):
    http, db, _ = client
    conversation = create(http, [])
    provider = model_id(http)
    state = http.get(f"/conversations/{conversation['id']}/context?provider_id={provider}").json()
    assert not state["protection_ready"] and state["capacity"] is None
    response = http.post(f"/conversations/{conversation['id']}/compact",
        json={"provider_id": provider, "request_id": str(uuid4())})
    assert response.status_code == 409


@pytest.mark.parametrize("settlement", [False, True])
def test_stop_waits_for_confirmed_usage_then_settles_once_without_running_tools(client, monkeypatch, settlement):
    from concurrent.futures import ThreadPoolExecutor
    from app.models import ModelBillingSettings, ModelCallUsage, ModelUsageRequest, UsageEvent
    from app.llm import ToolCall
    http, db, _ = client
    db.add(ModelBillingSettings(id=1, enabled=settlement)); db.commit()
    conversation = create(http, [])
    entered, release = Event(), Event()
    class Provider:
        last_usage = None
        def __init__(self, config):
            self.config = config
            self.status = OpenAICompatibleChatProvider(config=config).status
        def complete_with_tools(self, *_args, **_kwargs):
            raise AssertionError("must stream")
        def stream_with_tools(self, *_args, **_kwargs):
            yield "delta", {"text": "已收到的正文"}
            entered.set()
            assert release.wait(8)
            self.last_usage = LLMUsage(prompt_tokens=100, completion_tokens=20)
            yield "complete", ToolResponse("已收到的正文", [ToolCall("not-executed", "unavailable", {})], self.last_usage)
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", Provider)
    factory = sessionmaker(bind=db.get_bind())
    def database():
        with factory() as session:
            yield session
    main.app.dependency_overrides[main.get_db] = database
    request_id = str(uuid4())
    payload = {"request_id": request_id, "provider_id": model_id(http), "query": "hello"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(http.post, f"/conversations/{conversation['id']}/messages", json=payload)
        assert entered.wait(5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = http.get(f"/conversations/{conversation['id']}").json()
            if current["messages"][-1]["content"]:
                break
            time.sleep(.01)
        assert current["messages"][-1]["content"] == "已收到的正文"
        stopped = http.post(f"/conversations/{conversation['id']}/stop", json={"request_id": request_id})
        assert stopped.status_code == 200
        release.set()
        assert response.result(timeout=8).status_code == 200
    main.app.dependency_overrides[main.get_db] = lambda: db
    detail = http.get(f"/conversations/{conversation['id']}").json()
    assert detail["messages"][-1]["content"] == "已收到的正文"
    assert detail["messages"][-1]["status"] == "cancelled"
    assert detail["messages"][-1]["tool_events"] == []
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == UUID(request_id))).all()
    assert sum(event.credits for event in events) > 0
    balance = http.get("/usage").json()["balance"]
    assert http.post(f"/conversations/{conversation['id']}/stop", json={"request_id": request_id}).status_code == 200
    assert http.get("/usage").json()["balance"] == balance


def test_hard_budget_rejects_huge_current_input_without_upstream(client, monkeypatch):
    http, db, _ = client
    identifier = model_id(http)
    row = db.get(SiteModelProvider, UUID(identifier))
    row.context_capacity, row.max_output_tokens = 4096, 512; db.commit()
    calls = summary_provider(monkeypatch)
    conversation = create(http, [])
    events, _ = turn(http, conversation["id"], "超长问题" * 3000)
    assert events[-1][1]["status"] == "failed"
    assert "超出模型容量" in events[-1][1]["error_detail"]
    assert not calls


def manager_fixture(client, *, capacity=16000):
    from app import auth
    from app.auth import Access
    from app.llm_providers import request_provider_config
    from app.conversation_context import ContextManager
    from app.models import LoginSession
    http, db, conversation, provider = context_fixture(client, length=60)
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    access = Access(session.user_id, session.workspace_id, "admin", session.token_hash)
    request_id = uuid4()
    config = replace(request_provider_config(db, access.workspace_id, UUID(provider)),
        context_capacity=capacity, max_output_tokens=512)
    row = db.get(Conversation, UUID(conversation))
    row.active_request_id = request_id
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = ContextManager(factory, access, row.id, config, None, None, request_id)
    history = manager.history(db, row, list(row.messages))
    return manager, [{"role": "system", "content": "system-rules"}, *history, {"role": "user", "content": "current question"}]


def test_automatic_compaction_keeps_two_recent_complete_turns_and_current_input(client):
    from app.conversation_context import SUMMARY_PREFIX
    manager, messages = manager_fixture(client, capacity=7500)
    captured = []
    class Provider:
        def stream_with_tools(self, summary_messages, *_args, **_kwargs):
            captured.append(summary_messages)
            yield "complete", ToolResponse("关键事实：42，保留当前任务约束。", [])
    manager.provider = Provider()
    events = []
    compacted = manager.prepare(messages, [], events.append, lambda: None)
    assert len(captured) == 1 and compacted[0] == messages[0]
    assert compacted[1]["role"] == "user" and compacted[1]["content"].startswith(SUMMARY_PREFIX)
    assert [item["content"] for item in compacted[-5:]] == [item["content"] for item in messages[-5:]]
    assert events[-1][1]["status"] == "completed"


def test_same_task_compaction_only_replaces_complete_old_tool_groups(client):
    manager, _ = manager_fixture(client, capacity=10000)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current task"}]
    for index in range(5):
        messages.extend([{"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{index}", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"call-{index}", "content": "旧工具结果。" * 200}])
    class Provider:
        def stream_with_tools(self, *_args, **_kwargs):
            yield "complete", ToolResponse("已回查早期工具结果：42。", [])
    manager.provider = Provider()
    compacted = manager.prepare(messages, [], lambda _: None, lambda: None)
    assert compacted[:2] == messages[:2]
    assert compacted[-4:] == messages[-4:]
    assert len(compacted) == 7


def test_original_lookup_is_current_conversation_only_and_rechecks_access(client):
    from dataclasses import replace as copy
    from fastapi import HTTPException
    manager, _ = manager_fixture(client)
    result = manager.read_original({"start_sequence": 0, "end_sequence": 1})
    assert len(result["messages"]) == 2
    assert "error" in manager.read_original({"start_sequence": 0, "end_sequence": 20})
    manager.access = copy(manager.access, user_id=uuid4())
    with pytest.raises(HTTPException):
        manager.read_original({"start_sequence": 0, "end_sequence": 1})


@pytest.mark.parametrize("settlement", [False, True])
def test_confirmed_failed_stream_is_charged_and_insufficient_balance_becomes_pending(client, monkeypatch, settlement):
    from app.models import ModelBillingSettings, ModelUsageRequest, LEGACY_WORKSPACE_ID
    http, db, _ = client
    db.add(ModelBillingSettings(id=1, enabled=settlement))
    account = db.get(CreditAccount, LEGACY_WORKSPACE_ID)
    if account is None:
        from app.credits import ensure_account
        account = ensure_account(db, LEGACY_WORKSPACE_ID)
    account.balance = 2
    db.commit()
    calls = []
    class Provider:
        last_usage = None
        def __init__(self, config):
            self.config = config
            self.status = OpenAICompatibleChatProvider(config=config).status
        def complete_with_tools(self, *_args, **_kwargs):
            raise AssertionError("must stream")
        def stream_with_tools(self, *_args, **_kwargs):
            calls.append(1)
            yield "delta", {"text": "保留部分回答"}
            self.last_usage = LLMUsage(prompt_tokens=10000, completion_tokens=10000)
            raise LLMProviderError("controlled partial failure")
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", Provider)
    conversation = create(http, [])
    events, payload = turn(http, conversation["id"])
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["content"] == "保留部分回答"
    usage = http.get("/usage").json()
    assert usage["balance"] == 2 and usage["pending_credits"] > 2
    assert db.get(ModelUsageRequest, UUID(payload["request_id"])).status == "pending"
    denied = http.post(f"/conversations/{conversation['id']}/messages", json={
        "request_id": str(uuid4()), "provider_id": payload["provider_id"], "query": "again"})
    assert denied.status_code == 402 and len(calls) == 1


def test_restart_settles_durable_legacy_usage_and_marks_operations_interrupted(client):
    from types import SimpleNamespace
    from app import auth
    from app.models import LoginSession, LEGACY_WORKSPACE_ID, ContextOperation, UsageEvent, LLMProviderConfig
    from app.credits import authorization_hold
    from app.workspace_billing import operation_snapshot, LegacyPriceMeter
    http, db, _ = client
    conversation = create(http, [])
    binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == LEGACY_WORKSPACE_ID))
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    request_id = uuid4()
    operation_snapshot(db, LEGACY_WORKSPACE_ID, request_id, "model",
        capabilities=("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"), provider_id=binding.id)
    authorization_hold(db, LEGACY_WORKSPACE_ID, session.user_id, request_id, 1)
    db.get(Conversation, UUID(conversation["id"])).active_request_id = request_id
    db.add(ContextOperation(request_id=request_id, conversation_id=UUID(conversation["id"])))
    message = ChatMessage(conversation_id=UUID(conversation["id"]), sequence=0, role="assistant",
        request_id=request_id, status="streaming", content="已输出正文",
        tool_events=[{"call_id": "first", "status": "completed"}, {"call_id": "second", "status": "running"}],
        display_blocks=[{"id": "activity-second", "type": "activity", "call_id": "second"}])
    db.add(message)
    db.commit()
    provider = SimpleNamespace(status=SimpleNamespace(provider_id=str(binding.id), provider_key=binding.key))
    meter = LegacyPriceMeter(sessionmaker(bind=db.get_bind()), LEGACY_WORKSPACE_ID, request_id)
    meter.invoke(provider, lambda: ToolResponse("confirmed", [], LLMUsage(prompt_tokens=100, completion_tokens=20)), [])
    chat.recover_interrupted_conversations(db)
    assert db.get(ContextOperation, request_id).status == "interrupted"
    assert not db.get(Conversation, UUID(conversation["id"])).active_request_id
    db.refresh(message)
    assert message.status == "interrupted" and message.content == "已输出正文"
    assert [item["status"] for item in message.tool_events] == ["completed", "interrupted"]
    assert message.display_blocks[0]["call_id"] == "second"
    balance = http.get("/usage").json()["balance"]
    assert db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
    chat.recover_interrupted_conversations(db)
    assert http.get("/usage").json()["balance"] == balance


def test_model_budget_admin_validation_versions_and_public_redaction(client):
    from app.models import User
    http, db, _ = client
    identifier = model_id(http)
    assert http.get(f"/site/admin/llm-providers/{identifier}").status_code == 403
    user = db.get(User, UUID(http.get("/auth/me").json()["user"]["id"]))
    user.is_site_admin = True; db.commit()
    original = http.get(f"/site/admin/llm-providers/{identifier}").json()
    payload = {key: original[key] for key in ("connection_id", "connection_version", "name", "model", "version", "enabled", "thinking")}
    path = f"/site/admin/llm-providers/{identifier}"
    for changes in ({"context_capacity": 16000}, {"context_capacity": True, "max_output_tokens": 100},
                    {"context_capacity": 16000, "max_output_tokens": 15999}):
        assert http.put(path, json={**payload, **changes}).status_code == 422
    updated = http.put(path, json={**payload, "context_capacity": 16000, "max_output_tokens": 2000})
    assert updated.status_code == 200, updated.text
    assert updated.json()["context_capacity"] == 16000
    assert http.put(path, json={**payload, "context_capacity": 16000, "max_output_tokens": 2000}).status_code == 409
    response = http.get("/models/available")
    model = next(item for item in response.json()["providers"] if item["id"] == identifier)
    assert model["context_capacity"] == 16000
    assert model["max_output_tokens"] == 2000
    public = response.text
    assert "base_url" not in public and "api_key" not in public and "prices" not in public
    cleared = http.put(path, json={**payload, "version": updated.json()["version"]})
    assert cleared.status_code == 200
    model = next(item for item in http.get("/models/available").json()["providers"] if item["id"] == identifier)
    assert model["context_capacity"] is None and model["max_output_tokens"] is None
