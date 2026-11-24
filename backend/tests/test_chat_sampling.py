import json
import time
from http import HTTPStatus
from threading import Event, Thread
from uuid import uuid4

import pytest

from app.model_runtime import sampling
from app.conversations import jobs as conversation_jobs
from app.agent.loop import AgentCancelled
from app.model_runtime.client import ChatProviderConfig, LLMProviderError, OpenAICompatibleChatProvider, ToolResponse
from app.model_runtime.responses_socket import ResponsesSocket
from auth_helpers import generation_response, model_id
from test_conversation_streaming import sse, upstream
from test_conversations import client, create


def no_wait(_seconds, check):
    check()


def run(provider, report):
    return sampling.sample(lambda _: list(provider.stream_with_tools([{"role": "user", "content": "test"}], [])),
                           lambda: None, report, provider=provider)


def test_http_and_sampling_have_separate_budgets(monkeypatch):
    calls, waits, events = [], [], []
    monkeypatch.setattr(sampling, "wait", lambda seconds, check: waits.append(seconds))
    monkeypatch.setattr(sampling.random, "uniform", lambda *_: 1)

    def respond(handler, payload):
        calls.append(payload)
        handler.send_response(599)
        handler.send_header("Retry-After", "0.4")
        handler.end_headers()

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test"))
        with pytest.raises(sampling.SamplingError):
            run(provider, events.append)
    assert len(calls) == 30
    assert waits == [.4] * 29
    assert [event["attempt"] for event in events] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("status,code,retries", [(429, "rate_limit_exceeded", 5),
    (429, "insufficient_quota", 0), (400, "invalid_request_error", 0),
    (400, "context_length_exceeded", 0), (401, "invalid_api_key", 0)])
def test_classified_failures_do_not_enter_http_retry(monkeypatch, status, code, retries):
    calls, events, waits = [], [], []
    monkeypatch.setattr(sampling, "wait", lambda seconds, check: waits.append(seconds))

    def respond(handler, payload):
        calls.append(payload)
        handler.send_response(status)
        handler.send_header("Retry-After", "7")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": {"code": code}}).encode())

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test"))
        with pytest.raises(LLMProviderError) as error:
            run(provider, events.append)
    assert len(calls) == retries + 1
    assert waits == [7] * retries
    assert len(events) == retries
    if code == "context_length_exceeded":
        assert error.value.code == "provider_context_capacity_exceeded"


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_all_chat_adapters_discard_incomplete_calls_and_recover(monkeypatch, protocol):
    calls, observed = [], []
    monkeypatch.setattr(sampling, "wait", no_wait)

    def respond(handler, payload):
        calls.append(payload)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        complete = len(calls) > 1
        if protocol == "chat_completions":
            frames = [{"choices": [{"delta": {"content": "answer"}, **({"finish_reason": "stop"} if complete else {})}]}]
        elif protocol == "responses":
            frames = [{"type": "response.output_text.delta", "delta": "answer"}]
            if complete:
                frames.append({"type": "response.completed", "response": {"status": "completed", "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}]}})
            else:
                frames.append({"type": "response.function_call_arguments.delta", "delta": "{"})
        elif protocol == "anthropic":
            frames = [{"type": "message_start", "message": {"content": []}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}}]
            if complete:
                frames.extend([{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}])
        else:
            frames = [{"candidates": [{"content": {"parts": [{"text": "answer"}]},
                                       **({"finishReason": "STOP"} if complete else {})}]}]
        for frame in frames:
            sse(handler, frame)

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", api_protocol=protocol))
        events = run(provider, observed.append)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert observed[0]["attempt"] == 1
    assert events[-1][0] == "complete" and events[-1][1].content == "answer"
    assert events[-1][1].tool_calls == []


def test_stop_cancels_waiting_for_headers():
    entered, release, stopped = Event(), Event(), Event()

    def respond(handler, _):
        entered.set()
        release.wait(5)

    def check():
        if stopped.is_set():
            raise AgentCancelled()

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", timeout_seconds=.01))
        def stop():
            assert entered.wait(5)
            stopped.set()
        thread = Thread(target=stop)
        thread.start()
        try:
            with pytest.raises(AgentCancelled):
                sampling.sample(lambda _: list(provider.stream_with_tools([], [])), check, lambda _: None)
        finally:
            release.set()
            thread.join(5)


def test_sse_idle_is_per_event_and_does_not_limit_response_headers(monkeypatch):
    requests, recovery = [], []
    monkeypatch.setattr(sampling, "EVENT_IDLE_SECONDS", .35)
    monkeypatch.setattr(sampling, "wait", no_wait)

    def respond(handler, payload):
        requests.append(payload)
        first = len(requests) == 1
        if first:
            # Missing first SSE event triggers recovery after headers, even
            # though the same elapsed time waiting for headers is allowed.
            time.sleep(.5)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        handler.wfile.flush()
        if first:
            time.sleep(.5)
            return
        for _ in range(4):
            sse(handler, {"choices": [{"delta": {"content": "a"}}]})
            time.sleep(.12)
        sse(handler, {"choices": [{"delta": {}, "finish_reason": "stop"}]})

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", timeout_seconds=.01))
        events = run(provider, recovery.append)
    assert len(requests) == 2
    assert recovery[0]["attempt"] == 1
    assert events[-1][1].content == "aaaa"


def test_network_wait_budget_and_websocket_fallback(monkeypatch):
    waits, events = [], []
    monkeypatch.setattr(sampling, "wait", lambda seconds, check: waits.append(seconds))
    provider = OpenAICompatibleChatProvider(config=ChatProviderConfig("https://example.test/v1", "test", "test"))
    provider.responses_socket = ResponsesSocket()

    def invoke(attempt):
        if attempt == 0:
            raise sampling.SamplingError("switch", "http_fallback", after=2)
        if attempt <= 6:
            raise sampling.SamplingError("network", "network_wait")
        return attempt

    assert sampling.sample(invoke, lambda: None, events.append, provider=provider) == 7
    assert provider.responses_socket.fallback
    assert waits == [2, 5, 10, 20, 40, 60, 60]
    assert [event["category"] for event in events] == ["http_fallback", *(["network_wait"] * 6)]


def test_authentication_rechecks_rotated_credentials_without_using_retry_budget():
    from dataclasses import replace
    tokens, notices = [], []

    def respond(handler, _payload):
        tokens.append(handler.headers["Authorization"])
        if len(tokens) == 1:
            handler.send_response(401)
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        sse(handler, {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})

    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "old-test-key", "test"))

        def refresh():
            provider.config = replace(provider.config, api_key="new-test-key")
            return True

        provider.refresh_credentials = refresh
        assert run(provider, notices.append)[-1][1].content == "ok"
    assert tokens == ["Bearer old-test-key", "Bearer new-test-key"]
    assert notices == []


def test_responses_preserves_distinct_output_phases():
    from app.model_runtime.responses import ResponseStream
    state = ResponseStream(OpenAICompatibleChatProvider(config=ChatProviderConfig("https://example.test", "test", "test")))
    first = state.feed({"type": "response.output_text.delta", "delta": "先核对资料", "output_index": 0})
    final = state.feed({"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "phase": "commentary",
         "content": [{"type": "output_text", "text": "先核对资料"}]},
        {"type": "message", "role": "assistant", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "这是结论"}]},
    ]}})
    assert first + final[:-1] == [
        ("delta", {"text": "先核对资料", "item_index": 0}),
        ("text_phase", {"stage": "commentary", "item_index": 0}),
        ("delta", {"text": "这是结论", "item_index": 1}),
        ("text_phase", {"stage": "intermediate", "item_index": 1}),
    ]


def test_responses_websocket_continues_and_falls_back_with_same_route(monkeypatch):
    from app.model_runtime.responses import OUTPUT_FIELD
    from websockets.sync.server import serve
    received, https_requests, notices = [], [], []
    monkeypatch.setattr(sampling, "wait", no_wait)

    def socket_reply(connection):
        received.append(json.loads(connection.recv(timeout=3)))
        output = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "first"}]}
        connection.send(json.dumps({"type": "response.completed", "response": {
            "status": "completed", "id": "response-one", "output": [output]}}))
        received.append(json.loads(connection.recv(timeout=3)))
        connection.send(json.dumps({"type": "error", "code": "websocket_message_too_large"}))

    def http_reply(handler, payload):
        https_requests.append((payload, handler.headers["x-codex-turn-state"]))
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        sse(handler, {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "second"}]}]}})

    def route_header(_connection, _request, response):
        assert response.status_code == HTTPStatus.SWITCHING_PROTOCOLS
        response.headers["x-codex-turn-state"] = "test-route"
        return response

    with upstream(http_reply) as url, serve(socket_reply, "127.0.0.1", 0, process_response=route_header) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test", api_protocol="responses"))
        provider.responses_socket = ResponsesSocket()
        chat = [{"role": "user", "content": "original"}]

        def consume(_, messages):
            return list(provider.stream_with_tools(messages, []))[-1][1]

        # Only the adapter's socket endpoint points to the mock WebSocket, the
        # ordinary HTTP request still uses the provider's configured endpoint.
        original_events = provider.responses_socket.events
        socket_port = server.socket.getsockname()[1]
        monkeypatch.setattr(provider.responses_socket, "events",
            lambda _, key, body: original_events(f"http://127.0.0.1:{socket_port}/v1/responses", key, body))
        try:
            first = sampling.sample(lambda attempt: consume(attempt, chat), lambda: None, notices.append, provider=provider)
            followup = [*chat, {"role": "assistant", OUTPUT_FIELD: first.protocol_fields[OUTPUT_FIELD]},
                        {"role": "user", "content": "followup"}]
            final = sampling.sample(lambda attempt: consume(attempt, followup), lambda: None, notices.append, provider=provider)
        finally:
            provider.responses_socket.close()
            server.shutdown()
            thread.join(3)
    assert first.content == "first" and final.content == "second"
    assert received[1]["previous_response_id"] == "response-one"
    assert received[1]["input"] == [{"role": "user", "content": "followup"}]
    assert len(https_requests) == 1 and https_requests[0][1] == "test-route"
    assert "previous_response_id" not in https_requests[0][0]
    assert [item["category"] for item in notices] == ["http_fallback"]


def test_steering_is_idempotent_and_keeps_the_original_turn(client, monkeypatch):
    from app.conversations import execution as conversations
    http, db, _ = client
    chat = create(http, [])
    chat_id, turn, message_id = chat["id"], uuid4(), uuid4()
    calls = []
    path = f"/conversations/{chat_id}/requests/{turn}/steer"
    payload = {"message_id": str(message_id), "text": "补充要求"}

    def stream(self, messages, tools, **_):
        calls.append(messages)
        if len(calls) == 1:
            assert http.post(path, json=payload).json()["status"] == "pending"
            assert http.post(path, json=payload).json()["status"] == "pending"
        text = "初步结论" if len(calls) == 1 else "最终结论"
        yield "delta", {"text": text}
        yield "complete", ToolResponse(text, [])

    monkeypatch.setattr(conversation_jobs, "queue_title", lambda *_: None)
    monkeypatch.setattr(OpenAICompatibleChatProvider, "stream_with_tools", stream)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", OpenAICompatibleChatProvider)
    response = generation_response(http.post(f"/conversations/{chat_id}/messages", json={
        "provider_id": model_id(http), "request_id": str(turn), "query": "请分析"}))
    assert response.status_code == 200
    assert len(calls) == 2 and sum(item.get("content") == "补充要求" for item in calls[1]) == 1
    assert http.post(path, json=payload).json()["status"] == "accepted"
    assert http.post(path, json={**payload, "message_id": str(uuid4())}).status_code == 409
    messages = http.get(f"/conversations/{chat_id}").json()["messages"]
    blocks = [block for block in messages[-1]["display_blocks"] if block["type"] != "thinking"]
    assert [(block["type"], block.get("stage"), block["text"]) for block in blocks] == [
        ("text", "intermediate", "初步结论"), ("user", None, "补充要求"), ("text", "final", "最终结论")]
    assert messages[-1]["request_id"] == str(turn)
