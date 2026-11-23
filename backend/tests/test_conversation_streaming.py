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

from app import main
from app.conversations.jobs import recover_interrupted_conversations
from app.conversations import execution as chat
from app.model_runtime.client import ChatProviderConfig, LLMProviderError, LLMUsage, OpenAICompatibleChatProvider, ToolResponse
from app.models import BillingSnapshot, ChatMessage, ContextCheckpoint, Conversation, SiteModelProvider
from auth_helpers import model_id, generation_response
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


def test_real_multiturn_stream_normalizes_legacy_empty_tool_calls():
    from app.thinking import ThinkingSelection
    captured = []
    def respond(handler, payload):
        captured.append(payload)
        if any(message.get("tool_calls") == [] for message in payload["messages"]):
            handler.send_response(400); handler.end_headers()
            handler.wfile.write(b'{"error":{"message":"tool_calls: empty array"}}')
            return
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"choices": [{"delta": {"reasoning_content": "thinking"}}]})
        time.sleep(0.02)
        sse(handler, {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 3}})
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "deepseek",
            thinking=ThinkingSelection(adapter="deepseek", kind="fixed")))
        first = list(provider.stream_with_tools([{"role": "user", "content": "hello"}], []))[-1][1]
        legacy = {"role": "assistant", "content": first.content, "tool_calls": [], **first.protocol_fields}
        history = [{"role": "user", "content": "hello"}, legacy, {"role": "user", "content": "again"}]
        second = list(provider.stream_with_tools(history, []))[-1][1]
    assert second.content == "answer"
    assert legacy["tool_calls"] == []
    assert "tool_calls" not in captured[-1]["messages"][1]
    assert captured[-1]["messages"][1]["reasoning_content"] == "thinking"


def test_real_stream_http_error_remains_provider_error_and_does_not_echo_body():
    def respond(handler, _payload):
        handler.send_response(400); handler.send_header("Content-Type", "application/json"); handler.end_headers()
        handler.wfile.write(b'{"error":{"message":"sensitive-request-content"}}')
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test-only", "test"))
        with pytest.raises(LLMProviderError, match="HTTP 400") as error:
            list(provider.stream_with_tools([], []))
    assert "sensitive-request-content" not in str(error.value)


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
        with pytest.raises(LLMProviderError, match="输出长度上限"):
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
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("after_compaction", [False, True])
def test_api_sse_reaches_client_before_actual_upstream_closes(
    client, monkeypatch, disconnect, protocol, after_compaction,
):
    import uvicorn
    http, db, _ = client
    conversation = create(http, [])
    identifier = model_id(http)
    from app.models import ModelConnection
    selected = db.get(SiteModelProvider, UUID(identifier))
    selected.chat_protocol = protocol
    db.commit()
    if after_compaction:
        http, db, conversation_id, identifier = context_fixture(client, length=60, capacity=26000)
        conversation = {"id": conversation_id}
        summary_provider(monkeypatch)
        assert compact(http, conversation_id, identifier)[-1][1]["status"] == "completed"
    release, ended = Event(), Event()
    stop_signals = []
    bridge = chat.stream_events
    def observed_events(execution, stopped):
        stop_signals.append(stopped)
        return bridge(execution, stopped)
    monkeypatch.setattr(chat, "stream_events", observed_events)
    def respond(handler, _payload):
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"type": "response.output_text.delta", "delta": "首段正文"}
            if protocol == "responses" else {"choices": [{"delta": {"content": "首段正文"}}]})
        assert release.wait(8)
        if protocol == "responses":
            sse(handler, {"type": "response.output_text.delta", "delta": "后续正文"})
            sse(handler, {"type": "response.completed", "response": {"status": "completed",
                "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "首段正文后续正文"}]}],
                "usage": {"input_tokens": 8, "output_tokens": 4}}})
        else:
            sse(handler, {"choices": [{"delta": {"content": "后续正文"}, "finish_reason": "stop"}]})
            sse(handler, {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 4}})
        ended.set()
    factory = sessionmaker(bind=db.get_bind())
    from app.conversations import jobs as conversation_jobs
    workers = []
    def dispatch(identifier):
        worker = Thread(target=conversation_jobs.run, args=(identifier,),
            kwargs={"factory": factory}, daemon=True)
        workers.append(worker)
        worker.start()
    monkeypatch.setattr(conversation_jobs, "dispatch", dispatch)
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
            base = f"http://127.0.0.1:{listener.getsockname()[1]}/conversations/{conversation['id']}"
            headers = {**dict(http.headers), "Cookie": f"rag_session={http.cookies.get('rag_session')}"}
            submitted = httpx.post(base + "/messages", headers=headers,
                json={"request_id": str(uuid4()), "provider_id": identifier, "query": "test"}, timeout=10)
            assert submitted.status_code == 200, submitted.text
            with httpx.stream("GET", base + f"/requests/{submitted.json()['request_id']}/events",
                headers=headers, timeout=10) as response:
                assert response.status_code == 200
                stream = EventSource(response).iter_sse()
                first = next(event for event in stream if event.event == "delta")
                assert first.json()["text"] == "首段正文" and not ended.is_set()
                if not disconnect:
                    release.set()
                    remaining = list(stream)
                    assert remaining[-1].event == "done" and remaining[-1].json()["status"] == "completed"
            if disconnect:
                assert stop_signals and not stop_signals[0].is_set()
                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with factory() as session:
                        saved = session.scalar(select(ChatMessage).where(
                            ChatMessage.conversation_id == UUID(conversation["id"]), ChatMessage.role == "assistant")
                            .order_by(ChatMessage.sequence.desc()))
                        if saved.status != "streaming":
                            break
                    time.sleep(.02)
                assert saved.status == "completed" and saved.content == "首段正文后续正文"
                with factory() as session:
                    snapshots = session.scalars(select(BillingSnapshot).where(
                        BillingSnapshot.request_id == saved.request_id)).all()
                    assert sum(row.context.get("call_usage", {}).get("prompt_tokens") or 0 for row in snapshots) == 8
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=10)
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
            from app.model_runtime.client import ToolCall
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


def context_fixture(client, *, length=1000, capacity=16000):
    http, db, _ = client
    identifier = model_id(http)
    provider = db.get(SiteModelProvider, UUID(identifier))
    provider.context_capacity, provider.max_output_tokens = capacity, 2000
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
    return [(event.event, event.json()) for event in EventSource(generation_response(response)).iter_sse()]


def test_manual_compaction_preserves_budgeted_users_and_repeat_is_free(client, monkeypatch):
    # Allow the current system instructions and tool definitions plus the handoff.
    http, db, conversation, provider = context_fixture(client, length=60, capacity=26000)
    calls = summary_provider(monkeypatch)
    before = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    request_id = str(uuid4())
    events = compact(http, conversation, provider, request_id)
    assert events[-1][1]["status"] == "completed", events
    assert len(calls) == 1
    assert "record-3" in calls[0][1]["content"] and "record-4" in calls[0][1]["content"]
    after = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    assert after["estimated_input_tokens"] < before["estimated_input_tokens"]
    checkpoints = db.scalars(select(ContextCheckpoint)).all()
    assert len(checkpoints) == 1 and checkpoints[0].through_sequence == 7
    assert checkpoints[0].encrypted_replacement and checkpoints[0].window_id
    balance = http.get("/usage").json()["balance"]
    assert compact(http, conversation, provider)[-1][1]["no_op"]
    assert compact(http, conversation, provider, request_id)[-1][1]["status"] == "completed"
    assert len(calls) == 1 and http.get("/usage").json()["balance"] == balance
    detail = http.get(f"/conversations/{conversation}").json()
    assert len(detail["messages"]) == 8 and "record-0" in detail["messages"][0]["content"]
    assert not detail["active_request_id"]
    assert len(detail["compactions"]) == 1
    assert detail["compactions"][0]["after_sequence"] == 7
    assert "summary" not in detail["compactions"][0]


def test_personal_skill_checkpoint_survives_context_refresh_and_preserves_thinking(client, monkeypatch):
    from app import auth, mcp_client
    from app.models import AgentSkill, LoginSession
    from app.thinking import encrypt_display_thinking
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from types import SimpleNamespace
    key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_credential_key=key))
    http, db, conversation, provider = context_fixture(client, length=60, capacity=26000)
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    skill = AgentSkill(owner_id=session.user_id, account_id=auth.get_access(session).user_id,
        name="Test method", description="Check facts", markdown="Read and verify.", enabled=True)
    db.add(skill); db.flush()
    row = db.get(Conversation, UUID(conversation))
    row.skill_ids = [str(skill.id)]
    answer = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == row.id, ChatMessage.sequence == 1))
    answer.display_blocks = [{"id": "thinking-1", "type": "thinking", "round": 1, "status": "completed"}]
    answer.encrypted_display_thinking = encrypt_display_thinking([
        {"id": "thinking-1", "round": 1, "text": "original visible thinking"}])
    db.commit()
    calls = summary_provider(monkeypatch)
    context_url = f"/conversations/{conversation}/context?provider_id={provider}"
    thinking_url = f"/conversations/{conversation}/messages/{answer.id}/thinking"
    original = http.get(thinking_url).json()
    before = http.get(context_url).json()
    events = compact(http, conversation, provider)
    assert events[-1][1]["status"] == "completed"
    after = http.get(context_url).json()
    assert after["status"] == "completed"
    assert after["estimated_input_tokens"] < before["estimated_input_tokens"]
    assert http.get(thinking_url).json() == original
    assert "original visible thinking" not in http.get(f"/conversations/{conversation}").text
    assert compact(http, conversation, provider)[-1][1]["no_op"]
    assert len(calls) == 1


def test_failed_compaction_preserves_original_and_releases_hold(client, monkeypatch):
    http, db, conversation, provider = context_fixture(client, length=60, capacity=26000)
    calls = summary_provider(monkeypatch, fail=True)
    before = http.get("/usage").json()["balance"]
    events = compact(http, conversation, provider)
    assert events[-1][1]["status"] == "failed"
    assert len(calls) == 1
    assert db.scalars(select(ContextCheckpoint)).all() == []
    assert http.get("/usage").json()["balance"] == before
    assert not http.get(f"/conversations/{conversation}").json()["active_request_id"]


def test_display_edits_cannot_restore_replaced_history(client, monkeypatch):
    http, db, conversation, provider = context_fixture(client, length=60)
    summary_provider(monkeypatch)
    compact(http, conversation, provider)
    compressed = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    row = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == UUID(conversation), ChatMessage.sequence == 0))
    row.content += "changed " * 100; db.commit()
    current = http.get(f"/conversations/{conversation}/context?provider_id={provider}").json()
    assert current["estimated_input_tokens"] == compressed["estimated_input_tokens"]
    assert current["window_id"] == compressed["window_id"]


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
    from app.model_runtime.client import ToolCall
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
    assert http.post(f"/conversations/{conversation['id']}/messages", json=payload).status_code == 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        from app.conversations.jobs import run
        response = pool.submit(run, request_id, factory)
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
        response.result(timeout=8)
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


def manager_fixture(client, *, capacity=16000, messages=None, manual=False):
    from app import auth
    from app.auth import Access
    from app.llm_providers import request_provider_config
    from app.agent.effective_context import ContextManager
    from app.conversation_context import SUMMARY_PREFIX
    from app.context_windows import encrypt_items, make_item
    from app.models import LoginSession, ConversationContext
    http, db, conversation, provider = context_fixture(client, length=60)
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    access = auth.get_access(session)
    request_id = uuid4()
    config = replace(request_provider_config(db, access.user_id, UUID(provider)),
        context_capacity=capacity, max_output_tokens=512)
    row = db.get(Conversation, UUID(conversation))
    row.active_request_id = request_id
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = ContextManager(factory, access, row.id, config, None, None, request_id, manual=manual)
    manager.history(db, row, list(row.messages))
    if messages is not None:
        state = db.get(ConversationContext, row.id)
        state.encrypted_suffix = encrypt_items([make_item(uuid4(), item,
            "handoff" if str(item.get("content", "")).startswith(SUMMARY_PREFIX) else
            "user" if item["role"] == "user" else "tool" if item["role"] == "tool" else "model",
            request_id, config=config) for item in messages[1:]])
        db.commit()
        manager.history(db, row)
        manager.messages = messages
    else:
        db.commit()
        manager.current_message_id = uuid4()
        messages = manager.start({"role": "system", "content": "system-rules"}, "current question")
    db.commit()
    return manager, messages


def tool_group(identifier, text):
    return [{"role": "assistant", "content": None, "tool_calls": [
        {"id": identifier, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": identifier, "content": text}]


def test_automatic_compaction_keeps_budgeted_users_and_current_input(client):
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
    assert len(captured) == 1 and compacted[0]["content"] == "system-rules"
    assert sum(i.get("content") == "current question" for i in compacted) == 1
    assert any(str(i.get("content", "")).startswith(SUMMARY_PREFIX) for i in compacted)
    assert not any(i["role"] == "assistant" for i in compacted)
    assert events[-1][0] == "context" and events[-1][1]["status"] == "completed"


def test_same_task_compaction_preserves_latest_unconsumed_tool_group(client):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current task"}]
    for index in range(5):
        messages += tool_group(f"call-{index}", "旧工具结果。" * 200)
    original_last = messages[-2:]
    manager, messages = manager_fixture(client, capacity=10000, messages=messages)
    messages = manager.start(messages[0])
    manager.manual = True
    class Provider:
        def stream_with_tools(self, *_args, **_kwargs):
            yield "complete", ToolResponse("已回查早期工具结果：42。", [])
    manager.provider = Provider()
    compacted = manager.prepare(messages, [], lambda _: None, lambda: None)
    assert original_last[0] in compacted
    last = next(item for item in compacted if item.get("tool_call_id") == "call-4")
    projection = json.loads(last["content"])
    assert projection["truncated"]
    assert original_last[1]["content"].startswith(projection["preview"])
    saved = manager.read_result({"result_id": projection["result_id"]})
    assert original_last[1]["content"].startswith(saved["text"])
    assert len(compacted) == 5
    assert "call-0" not in str(compacted)


@pytest.mark.parametrize("images", [False, True])
def test_compaction_retains_current_multimodal_input_and_atomic_latest_group(client, images):
    content = [{"type": "text", "text": "current task"},
        {"type": "chat_image", "attachment_id": "image-1", "filename": "one.png", "width": 1818, "height": 865},
        {"type": "chat_image", "attachment_id": "image-2", "filename": "two.png", "width": 1774, "height": 953}] if images else "current task"
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": content}]
    messages += tool_group("catalog", "one document")
    messages += tool_group("search", "重要资料。" * (6000 if images else 1300))
    messages += tool_group("error", "invalid document ID")
    manager, messages = manager_fixture(client, capacity=50000 if images else 10000, messages=messages)
    class Provider:
        def stream_with_tools(self, *_args, **_kwargs):
            yield "complete", ToolResponse("旧工具记录摘要", [])
    manager.provider = Provider()
    compacted = manager.prepare(messages, [], lambda _: None, lambda: None)
    assert compacted[1]["content"] == content
    assert any(i.get("tool_call_id") == "error" for i in compacted)
    assert "重要资料" not in str(compacted)


def test_failed_compaction_does_not_retry_unchanged_window(client):
    from app.model_runtime.client import LLMConfigurationError
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    messages += tool_group("old", "内容。" * 2100)
    messages += tool_group("recent", "recent result")
    manager, messages = manager_fixture(client, capacity=14000, messages=messages)
    calls = []
    class Provider:
        def stream_with_tools(self, *args, **kwargs):
            calls.append(1)
            yield "complete", ToolResponse("更多内容。" * 3000, [])
    manager.provider = Provider()
    events = []
    with pytest.raises(LLMConfigurationError):
        manager.prepare(messages, [], events.append, lambda: None)
    assert [d["status"] for n, d in events if n == "compaction"] == ["running", "failed"]
    try:
        manager.prepare(messages, [], events.append, lambda: None)
    except LLMConfigurationError:
        pass
    assert len(calls) == 1
    with manager.factory() as db:
        assert not db.scalars(select(ContextCheckpoint)).all()


def test_over_budget_summary_never_persists_or_emits_completed(client):
    from app.model_runtime.client import LLMConfigurationError
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current task " * 600}]
    messages += tool_group("old", "原始结果。" * 850)
    messages += tool_group("recent", "recent result")
    manager, messages = manager_fixture(client, capacity=10000, messages=messages)
    class Provider:
        def stream_with_tools(self, *args, **kwargs):
            yield "complete", ToolResponse("仍然很长。" * 750, [])
    manager.provider = Provider()
    events = []
    with pytest.raises(LLMConfigurationError):
        manager.prepare(messages, [], events.append, lambda: None)
    assert [d["status"] for n, d in events if n == "compaction"] == ["running", "failed"]
    with manager.factory() as db:
        assert not db.scalars(select(ContextCheckpoint)).all()


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
    from app.credits import ensure_account
    from app.models import ModelBillingSettings, ModelUsageRequest
    http, db, _ = client
    db.add(ModelBillingSettings(id=1, enabled=settlement))
    user_id = UUID(http.get("/auth/me").json()["user"]["id"])
    account = ensure_account(db, user_id)
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
    from app.models import LoginSession, ContextOperation, UsageEvent, LLMProviderConfig
    from app.credits import authorization_hold
    from app.workspace_billing import operation_snapshot, LegacyPriceMeter
    http, db, _ = client
    conversation = create(http, [])
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.account_id == session.user_id))
    request_id = uuid4()
    operation_snapshot(db, session.user_id, request_id, "model",
        capabilities=("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"), provider_id=binding.id)
    authorization_hold(db, session.user_id, session.user_id, request_id, 1)
    db.get(Conversation, UUID(conversation["id"])).active_request_id = request_id
    db.add(ContextOperation(request_id=request_id, conversation_id=UUID(conversation["id"])))
    message = ChatMessage(conversation_id=UUID(conversation["id"]), sequence=0, role="assistant",
        request_id=request_id, status="streaming", content="已输出正文",
        tool_events=[{"call_id": "first", "status": "completed"}, {"call_id": "second", "status": "running"}],
        display_blocks=[{"id": "activity-second", "type": "activity", "call_id": "second"}])
    db.add(message)
    db.commit()
    provider = SimpleNamespace(status=SimpleNamespace(provider_id=str(binding.id), provider_key=binding.key))
    meter = LegacyPriceMeter(sessionmaker(bind=db.get_bind()), session.user_id, request_id)
    meter.invoke(provider, lambda: ToolResponse("confirmed", [], LLMUsage(prompt_tokens=100, completion_tokens=20)), [])
    recover_interrupted_conversations(db)
    assert db.get(ContextOperation, request_id).status == "interrupted"
    assert not db.get(Conversation, UUID(conversation["id"])).active_request_id
    db.refresh(message)
    assert message.status == "interrupted" and message.content == "已输出正文"
    assert [item["status"] for item in message.tool_events] == ["completed", "interrupted"]
    assert message.display_blocks[0]["call_id"] == "second"
    balance = http.get("/usage").json()["balance"]
    assert db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
    recover_interrupted_conversations(db)
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
