import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from e2b.connection_config import ConnectionConfig
from e2b.sandbox_sync.filesystem.filesystem import Filesystem
from packaging.version import Version

from app import upstream_retries as retry
from app.agent import AgentCancelled
from app.cloud_computer_adapter import AlibabaSandbox
from app.image_reading import ImageReadError, read_image
from app.llm import ChatProviderConfig, LLMProviderError, OpenAICompatibleChatProvider
from test_cloud_computer_adapter import FileDownload
from test_chat_attachments import PNG
from test_conversation_streaming import upstream, sse
from test_conversations import client, create
from auth_helpers import model_id, generation_response


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 100.
        waits = []

        def sleep(self, delay):
            assert 0 <= delay <= .1
            self.now += delay
            self.waits.append(delay)

    value = Clock()
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: value.now, sleep=value.sleep))
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: high)
    return value


def provider(protocol="chat_completions"):
    return OpenAICompatibleChatProvider(config=ChatProviderConfig(
        "https://example.test/v1", "test-key", "test", api_protocol=protocol))


def test_budget_retry_after_jitter_and_total_limit(clock, monkeypatch):
    events = []
    with retry.retry_scope(notify=events.append):
        budget = retry.RetryBudget("model")
        assert budget.begin()
        assert budget.delay("http_429", 3) == 3
        budget.wait(3)
        assert budget.begin()
        assert budget.delay("http_503") == 2
        assert budget.delay("http_429", 9) is None
        clock.now += 26
        assert budget.delay("http_503", 2) is None
    assert events[0]["attempt"] == 2 and events[0]["wait_seconds"] == 3
    assert retry.retry_after("NaN") is None
    assert retry.retry_after("inf") is None
    assert retry.retry_after("invalid") is None
    assert retry.retry_after("-1") == 0
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=4), usegmt=True)
    assert 2 <= retry.retry_after(date) <= 4
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: low)
    budget = retry.RetryBudget("model")
    budget.begin()
    assert budget.delay("http_500") == .5


@pytest.mark.parametrize("stop", [AgentCancelled, PermissionError])
def test_wait_observes_cancellation_and_revocation(clock, stop):
    def check():
        if clock.now > 100.2:
            raise stop()
    with retry.retry_scope(check):
        budget = retry.RetryBudget("model")
        budget.begin()
        with pytest.raises(stop):
            budget.wait(5)
    assert clock.now <= 100.4


def test_task_deadline_and_attempt_limit(clock):
    with retry.retry_scope(deadline=100.4):
        budget = retry.RetryBudget("model")
        assert budget.begin()
        assert budget.delay("http_503") is None
        clock.now += 1
        assert not budget.begin()
    budget = retry.RetryBudget("model", policy=retry.RetryPolicy(attempts=1))
    assert budget.begin() and budget.delay("http_429") is None


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("status", [429, 500, 503])
def test_all_protocols_retry_rejected_calls_then_use_same_payload(clock, monkeypatch, protocol, status):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if len(calls) < 3:
            return httpx.Response(status, headers={"Retry-After": "2"}, json={"error": "secret"})
        bodies = {"chat_completions": {"choices": [{"message": {"content": "完成"}}]},
            "responses": {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "完成"}]}]},
            "anthropic": {"content": [{"type": "text", "text": "完成"}], "stop_reason": "end_turn"},
            "gemini": {"candidates": [{"content": {"parts": [{"text": "完成"}]}, "finishReason": "STOP"}]}}
        return httpx.Response(200, json=bodies[protocol])
    monkeypatch.setattr(httpx, "post", post)
    assert provider(protocol).complete([{ "role": "user", "content": "问题"}]) == "完成"
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert sum(clock.waits) == pytest.approx(4)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
def test_nonrecoverable_model_status_is_not_retried(clock, monkeypatch, status):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return httpx.Response(status, json={"error": "private-body"})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(LLMProviderError, match=f"HTTP {status}") as raised:
        provider().complete([])
    assert "private-body" not in str(raised.value)
    assert len(calls) == 1 and not clock.waits


@pytest.mark.parametrize("error,retried", [(httpx.ConnectError, True), (httpx.ConnectTimeout, True),
    (httpx.PoolTimeout, True), (httpx.ReadTimeout, False), (httpx.WriteTimeout, False),
    (httpx.ReadError, False), (httpx.RemoteProtocolError, False)])
def test_model_transport_failure_does_not_replay_uncertain_requests(clock, monkeypatch, error, retried):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        raise error("private-key")
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(LLMProviderError) as raised:
        provider().complete([])
    assert len(calls) == (3 if retried else 1)
    assert "private-key" not in str(raised.value)
    if not retried:
        assert "用量尚未完全确认" in str(raised.value)


@contextmanager
def sdk_files(respond):
    files = Filesystem("https://sandbox.test", Version("0.4.0"), ConnectionConfig())
    client = httpx.Client(base_url="https://sandbox.test", transport=httpx.MockTransport(respond))
    try:
        with patch("app.cloud_computer_adapter.image_file_client", return_value=client):
            yield SimpleNamespace(files=files)
        assert client.event_hooks["response"] == []
    finally:
        client.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_real_sdk_error_types_recover_and_read_complete_image(clock, status):
    calls, downloads = [], []
    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(status, headers={"Retry-After": "2"}, json={"message": "private details"})
        stream = FileDownload([PNG[:10], PNG[10:]])
        downloads.append(stream)
        return httpx.Response(200, stream=stream)
    with sdk_files(respond) as sandbox:
        data = AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, "/tmp/chart.png", max_bytes=1024)
    assert data == PNG and downloads[0].closed
    assert len(calls) == 3 and sum(clock.waits) == pytest.approx(4)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_sdk_nonrecoverable_errors_never_retry(clock, status):
    from fastapi import HTTPException
    from e2b import AuthenticationException
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "private details"})
    with sdk_files(respond) as sandbox:
        with pytest.raises(AuthenticationException if status == 401 else HTTPException if status == 403 else ImageReadError) as raised:
            AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, "x", max_bytes=1024)
    assert len(calls) == 1 and not clock.waits
    if status != 401:  # Authentication remains a propagated permission boundary.
        assert "private details" not in str(raised.value)


def test_sdk_partial_stream_is_discarded_and_both_streams_closed(clock):
    downloads = [FileDownload([b"bad-prefix"], httpx.ReadError("private details")),
        FileDownload([PNG[:10], PNG[10:]])]
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=downloads[len(calls)-1])
    with sdk_files(respond) as sandbox:
        data = AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, "x", max_bytes=1024)
    assert data == PNG and all(value.closed for value in downloads)


def test_sdk_retry_exhaustion_is_safe_tool_result_and_logs(clock, caplog):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, json={"message": "PRIVATE-CONTENT"})
    with sdk_files(respond) as sandbox:
        def load(path):
            return AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, path, max_bytes=1024), {}
        result = read_image({"path": "x"}, config=ChatProviderConfig("https://test", "secret", "vision", supports_images=True),
            local_loader=load)
    assert result["error_code"] == "image_transfer_failed" and "服务暂时不可用" in result["error"]
    assert len(calls) == 3
    assert "PRIVATE-CONTENT" not in json.dumps(result) + caplog.text


def test_stream_http_retries_before_acceptance_and_never_after(clock):
    calls = []
    def respond(handler, payload):
        calls.append(payload)
        if len(calls) < 3:
            handler.send_response(429); handler.end_headers()
            return
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "部分"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2}})
    with upstream(respond) as url:
        value = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test"))
        stream = value.stream_with_tools([], [])
        assert next(stream) == ("delta", {"text": "部分"})
        with pytest.raises(LLMProviderError):
            list(stream)
    assert len(calls) == 3 and value.last_usage.prompt_tokens == 8


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("async_text", [False, True])
def test_real_model_http_streams_retry_then_finish(clock, protocol, async_text):
    calls = []
    def respond(handler, payload):
        calls.append(payload)
        if len(calls) < 3:
            handler.send_response(503); handler.send_header("Retry-After", "1"); handler.end_headers()
            return
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        if protocol == "chat_completions":
            sse(handler, {"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]})
            handler.wfile.write(b"data: [DONE]\n\n"); handler.wfile.flush()
        elif protocol == "responses":
            sse(handler, {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "完成"}]}]}})
        elif protocol == "gemini":
            sse(handler, {"candidates": [{"content": {"parts": [{"text": "完成"}]}, "finishReason": "STOP"}]})
        else:
            for event in [{"type": "message_start", "message": {"content": [], "usage": {}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "完成"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
                {"type": "message_stop"}]:
                sse(handler, event)
    # Use a controllable async clock as well as the synchronous clock.
    from unittest.mock import patch
    async def sleep(delay):
        clock.sleep(delay)
    with upstream(respond) as url, patch.object(retry.asyncio, "sleep", sleep):
        value = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", api_protocol=protocol))
        if async_text:
            async def collect():
                return "".join([part async for part in value.stream([])])
            assert asyncio.run(collect()) == "完成"
        else:
            assert list(value.stream_with_tools([], []))[-1][1].content == "完成"
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]


@pytest.mark.parametrize("during_wait", [False, True])
def test_cancel_or_revoke_before_next_model_attempt_closes_response(clock, monkeypatch, during_wait):
    from fastapi import HTTPException
    responses = []
    active = True
    def check():
        if not active or (during_wait and clock.now > 100.2):
            raise HTTPException(403, "撤权")
    @contextmanager
    def stream(*args, **kwargs):
        response = httpx.Response(429)
        responses.append(response)
        try:
            yield response
        finally:
            response.close()
    def notify(_info):
        nonlocal active
        if not during_wait:
            active = False
    monkeypatch.setattr(httpx, "stream", stream)
    with retry.retry_scope(check, notify):
        with pytest.raises(HTTPException):
            list(provider().stream_with_tools([], []))
    assert len(responses) == 1 and responses[0].is_closed


def test_retry_after_over_budget_stops_without_waiting(clock, monkeypatch):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(LLMProviderError, match="HTTP 429"):
        provider().complete([])
    assert len(calls) == 1 and not clock.waits


@pytest.mark.parametrize("mode", ["post", "stream", "async_stream"])
@pytest.mark.parametrize("reject_first", [False, True])
def test_slow_success_preserves_configured_timeout_past_retry_window(monkeypatch, mode, reject_first):
    # Scale down the window, not the configured timeout: exercise real socket reads.
    policy = retry.RetryPolicy(seconds=.15, base_delay=.01, max_delay=.02)
    monkeypatch.setattr(retry, "RetryPolicy", lambda: policy)
    calls = []
    def respond(handler, payload):
        calls.append(payload)
        if reject_first and len(calls) == 1:
            handler.send_response(503)
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(.3)
        try:
            handler.wfile.write(b"completed")
            handler.wfile.flush()
        except BrokenPipeError:
            pass
    value = SimpleNamespace(timeout_seconds=2, check_available=lambda: None)
    with upstream(respond) as url:
        if mode == "post":
            response = retry.model_post(value, url, json={}, timeout=2)
            try:
                assert response.text == "completed"
            finally:
                response.close()
        elif mode == "stream":
            with retry.model_stream(value, "POST", url, json={}, timeout=2) as response:
                assert response.read() == b"completed"
        else:
            async def collect():
                async with httpx.AsyncClient() as client:
                    async with retry.model_astream(value, client, "POST", url, json={}, timeout=2) as response:
                        return await response.aread()
            assert asyncio.run(collect()) == b"completed"
    assert len(calls) == (2 if reject_first else 1)
    assert value.request_observation["accepted_response"]


@pytest.mark.parametrize("mode", ["post", "stream", "async_stream"])
@pytest.mark.parametrize("configured,expected", [(60, 40), (5, 5)])
def test_explicit_task_deadline_still_bounds_model_timeout(clock, monkeypatch, mode, configured, expected):
    captured = []
    value = SimpleNamespace(timeout_seconds=60, check_available=lambda: None)
    def response(**kwargs):
        captured.append(kwargs["timeout"])
        return httpx.Response(200)
    if mode == "post":
        monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(**kwargs))
    elif mode == "stream":
        @contextmanager
        def stream(*args, **kwargs):
            yield response(**kwargs)
        monkeypatch.setattr(httpx, "stream", stream)
    else:
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def stream(*args, **kwargs):
            yield response(**kwargs)
        client = SimpleNamespace(stream=stream)
    with retry.retry_scope(deadline=clock.now + 40):
        if mode == "post":
            retry.model_post(value, "https://test", timeout=configured).close()
        elif mode == "stream":
            with retry.model_stream(value, "POST", "https://test", timeout=configured):
                pass
        else:
            async def collect():
                async with retry.model_astream(value, client, "POST", "https://test", timeout=configured):
                    pass
            asyncio.run(collect())
    assert captured == [expected]


def test_retry_transport_disables_sdk_inner_connection_retries(monkeypatch):
    from app import cloud_computer_adapter as adapter
    captured = []
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: captured.append(kwargs) or httpx.MockTransport(
        lambda request: httpx.Response(200)))
    files = Filesystem("https://sandbox.test", Version("0.4.0"), ConnectionConfig())
    with adapter.image_file_client(files) as client:
        assert client.get("/files").status_code == 200
    assert captured[0]["retries"] == 0


def test_sdk_retry_changed_image_still_fails_digest_validation(clock):
    import hashlib
    attempts = []
    def respond(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) == 1 else httpx.Response(200, content=PNG)
    with sdk_files(respond) as sandbox:
        def load(path):
            return AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, path, max_bytes=1024), {
                "size_bytes": len(PNG), "sha256": hashlib.sha256(b"old-image").hexdigest()}
        result = read_image({"path": "x"}, config=ChatProviderConfig("https://test", "test", "test", supports_images=True),
            local_loader=load)
    assert result["error_code"] == "image_changed" and len(attempts) == 2


@pytest.mark.parametrize("metering", [False, True])
def test_durable_agent_retries_only_failed_model_round_and_reconnect_does_not_execute(client, monkeypatch, clock, metering):
    from uuid import UUID, uuid4
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app import conversation_jobs, conversations
    from app.models import BillingSnapshot, ChatMessage, ModelCallUsage, ModelBillingSettings
    http, db, _ = client
    conversation = create(http, [])
    if metering:
        settings = db.get(ModelBillingSettings, 1)
        if settings is None:
            settings = ModelBillingSettings(id=1)
            db.add(settings)
        settings.enabled = True
        db.commit()
    calls, tools_run = [], []
    class Extensions:
        skills, tools, versions = [], [], []
        environment_description = "隔离测试"
        def __init__(self, *args, **kwargs): pass
        def label(self, name, arguments=None): return "执行测试命令"
        def execute(self, name, arguments, check_active=None, **kwargs):
            tools_run.append(name)
            return {"status": "completed"}
    def respond(handler, payload):
        calls.append(payload)
        if len(calls) in (2, 3):
            handler.send_response(429); handler.end_headers()
            return
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        if len(calls) == 1:
            sse(handler, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "command", "function": {
                "name": "run_command", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2}})
        else:
            sse(handler, {"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    monkeypatch.setattr(conversations, "ExtensionRuntime", Extensions)
    monkeypatch.setattr(conversation_jobs, "queue_title", lambda *args: None)
    with upstream(respond) as url:
        monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda **kwargs: OpenAICompatibleChatProvider(
            config=__import__("dataclasses").replace(kwargs["config"], base_url=url, site_provider_id=None)))
        request_id = str(uuid4())
        response = generation_response(http.post(f"/conversations/{conversation['id']}/messages", json={
            "request_id": request_id, "provider_id": model_id(http), "query": "执行测试"}))
        assert response.status_code == 200 and "event: done" in response.text
        assert len(calls) == 4 and tools_run == ["run_command"]
        message = db.scalar(select(ChatMessage).where(ChatMessage.request_id == UUID(request_id)))
        db.refresh(message)
        assert message.status == "completed" and message.content == "完成"
        activity = next(item for item in message.tool_events if item["name"] == "model_request")
        assert activity["status"] == "completed" and activity["summary"] == "请求已恢复"
        assert "秒后重试" in response.text and "正在重试" in response.text
        snapshots = list(db.scalars(select(BillingSnapshot).where(BillingSnapshot.request_id == UUID(request_id))))
        uses = [row.context["call_usage"] for row in snapshots if "call_usage" in row.context]
        if metering:
            records = list(db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == UUID(request_id))
                .order_by(ModelCallUsage.round)))
            uses = [item.usage for item in records]
        assert len(uses) == 2 and sum(item["prompt_tokens"] for item in uses) == 18
        assert uses[-1]["thinking_diagnostic"]["upstream"]["attempts"] == 3
        reconnect = http.get(f"/conversations/{conversation['id']}/requests/{request_id}/events")
        assert reconnect.status_code == 200 and "event: done" in reconnect.text
        conversation_jobs.run(request_id, sessionmaker(bind=db.get_bind()))
        assert len(calls) == 4 and tools_run == ["run_command"]


def test_restart_interrupts_waiting_activity_without_dispatch_or_charging_twice(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from uuid import UUID, uuid4
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app import conversation_jobs
    from app.models import ChatMessage, ConversationExecution, Conversation
    http, db, _ = client
    conversation = create(http, [])
    request_id = uuid4()
    assert http.post(f"/conversations/{conversation['id']}/messages", json={
        "request_id": str(request_id), "provider_id": model_id(http), "query": "等待测试"}).status_code == 200
    execution = db.get(ConversationExecution, request_id)
    execution.status = "running"
    execution.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    message = db.get(ChatMessage, execution.message_id)
    message.tool_events = [{"call_id": "model-retry-1", "name": "model_request", "label": "请求回答",
        "kind": "model", "status": "running", "summary": "服务暂时繁忙，1 秒后重试（第 2/3 次）"}]
    message.display_blocks = [{"id": "activity-model-retry-1", "type": "activity", "call_id": "model-retry-1"}]
    db.commit()
    dispatched = []
    monkeypatch.setattr(conversation_jobs, "dispatch", dispatched.append)
    monkeypatch.setattr(conversation_jobs, "queue_title", lambda *args: None)
    factory = sessionmaker(bind=db.get_bind())
    conversation_jobs.recover(factory)
    db.expire_all()
    assert db.get(ConversationExecution, request_id).status == "interrupted"
    assert db.get(ChatMessage, execution.message_id).tool_events[0]["status"] == "interrupted"
    assert db.get(Conversation, UUID(conversation["id"])).active_request_id is None
    conversation_jobs.recover(factory)
    conversation_jobs.run(request_id, factory)
    assert dispatched == []


def test_nested_scope_cannot_extend_parent_deadline(clock):
    with retry.retry_scope(deadline=100.2):
        with retry.retry_scope(deadline=120):
            budget = retry.RetryBudget("sandbox_file")
            assert budget.deadline == 100.2


def test_compaction_recovery_clears_waiting_summary(client, monkeypatch, clock):
    from dataclasses import replace
    from test_conversation_streaming import manager_fixture
    manager, messages = manager_fixture(client, capacity=7500)
    calls, events = [], []
    def respond(handler, payload):
        calls.append(payload)
        handler.send_response(429 if len(calls) == 1 else 200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if len(calls) > 1:
            sse(handler, {"choices": [{"delta": {"content": "关键事实：42。"}, "finish_reason": "stop"}]})
    with upstream(respond) as url:
        manager.provider = OpenAICompatibleChatProvider(config=replace(manager.config, base_url=url, site_provider_id=None))
        manager.prepare(messages, [], events.append, lambda: None)
    assert len(calls) == 2
    activity = {}
    for name, data in events:
        if data.get("call_id") == "compact-1":
            activity.update(data)
    assert any("秒后重试" in data.get("summary", "") for _, data in events)
    assert activity["status"] == "completed" and activity["summary"] == ""


@pytest.mark.parametrize("stop", [AgentCancelled, PermissionError])
def test_sdk_wait_cancel_or_revoke_closes_client_and_prevents_next_attempt(clock, stop):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    def check():
        if clock.now > 100.2:
            raise stop()
    with sdk_files(respond) as sandbox:
        with pytest.raises(stop):
            AlibabaSandbox.__new__(AlibabaSandbox).read_file(sandbox, "x", max_bytes=1024, check_active=check)
    assert len(calls) == 1


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("async_text", [False, True])
def test_accepted_partial_text_tool_and_usage_never_replayed(clock, protocol, async_text):
    calls = []
    def respond(handler, payload):
        calls.append(payload)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if protocol == "chat_completions":
            events = [{"choices": [{"delta": {"content": "部分", "tool_calls": [{"index": 0,
                "id": "tool", "function": {"name": "run_command", "arguments": "{"}}]}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2}}]
        elif protocol == "responses":
            events = [{"type": "response.output_text.delta", "delta": "部分"},
                {"type": "response.function_call_arguments.delta", "delta": "{"},
                {"type": "response.incomplete", "response": {"status": "incomplete",
                    "usage": {"input_tokens": 7, "output_tokens": 2}}}]
        elif protocol == "anthropic":
            events = [{"type": "message_start", "message": {"content": [], "usage": {"input_tokens": 7}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "部分"}},
                {"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": "tool", "name": "run_command", "input": {}}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{"}},
                {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}}]
        else:
            events = [{"candidates": [{"content": {"parts": [{"text": "部分"},
                {"functionCall": {"name": "run_command", "args": {}}}]}}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}}]
        for event in events:
            sse(handler, event)
    with upstream(respond) as url:
        value = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", api_protocol=protocol))
        if async_text:
            parts = []
            async def collect():
                async for part in value.stream([]):
                    parts.append(part)
            with pytest.raises(LLMProviderError):
                asyncio.run(collect())
            assert parts == ["部分"]
        else:
            events = value.stream_with_tools([], [])
            assert next(events) == ("delta", {"text": "部分"})
            with pytest.raises(LLMProviderError):
                list(events)
        assert value.last_usage.prompt_tokens == 7 and value.last_usage.completion_tokens == 2
        assert value.request_observation["accepted_response"]
    assert len(calls) == 1 and not clock.waits
