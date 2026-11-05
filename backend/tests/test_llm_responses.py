import asyncio
import json
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.llm import ChatProviderConfig, LLMConfigurationError, LLMProviderError, OpenAICompatibleChatProvider
from app.llm_responses import OUTPUT_FIELD, ResponseStream, api_root, endpoint, result, usage
from app.thinking import encrypt_continuation, history_message, select_thinking
from test_conversation_streaming import upstream, sse
from test_thinking import effort
from test_conversations import client, create, turn


def config(**kwargs):
    return ChatProviderConfig("https://example.test/v1", "test-only", "test",
        api_protocol="responses", **kwargs)


def body(output=None, status="completed"):
    return {"id": "resp-1", "status": status, "output": output if output is not None else [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}],
        "usage": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
                  "input_tokens_details": {"cached_tokens": 60},
                  "output_tokens_details": {"reasoning_tokens": 20}}}


def test_payload_converts_messages_tools_thinking_and_limits_without_changing_schema():
    provider = OpenAICompatibleChatProvider(config=config(
        max_output_tokens=1024, thinking=select_thinking(effort("openai"), 1)))
    tools = [{"type": "function", "function": {"name": "search", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}, "optional": {"type": "integer"}},
        "required": ["query"]}}}]
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1",
            "function": {"name": "search", "arguments": '{"query":"q"}'}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "found"}]
    payload = provider._payload(messages, tools=tools, tool_choice={"type": "function", "function": {"name": "search"}}, stream=True)
    assert payload["store"] is False and payload["include"] == ["reasoning.encrypted_content"]
    assert payload["input"][-1] == {"type": "function_call_output", "call_id": "call-1", "output": "found"}
    assert payload["input"][-2]["call_id"] == "call-1"
    assert payload["tools"][0]["strict"] is False
    assert payload["tools"][0]["parameters"]["required"] == ["query"] and "strict" not in tools[0]["function"]
    assert payload["tool_choice"] == {"type": "function", "name": "search"}
    assert payload["reasoning"] == {"effort": "high"} and payload["max_output_tokens"] == 1024
    assert not {"messages", "reasoning_effort", "max_tokens", "max_completion_tokens", "stream_options", "temperature"} & payload.keys()


def test_nonstreaming_routes_explicit_endpoint_and_never_retries_another_protocol(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return httpx.Response(200, json=body())
    monkeypatch.setattr(httpx, "post", post)
    provider = OpenAICompatibleChatProvider(config=config())
    assert provider.complete([]) == "answer"
    assert provider.complete_with_tools([], []).usage.cached_input_tokens == 60
    assert all(url == "https://example.test/v1/responses" for url, _ in calls)
    assert provider.last_usage.measured()["input_tokens"] == 40
    assert provider.last_usage.completion_tokens == 25 and provider.last_usage.reasoning_tokens == 20
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: (calls.append(a[0]) or httpx.Response(404)))
    with pytest.raises(LLMProviderError):
        provider.complete([])
    assert len(calls) == 3


def test_real_responses_stream_delivers_before_completion_and_assembles_tool_arguments():
    release, ended = Event(), Event()
    def respond(handler, payload):
        assert handler.path == "/v1/responses" and payload["stream"] and not payload["store"]
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        sse(handler, {"type": "response.output_text.delta", "delta": "answer"})
        assert release.wait(5)
        sse(handler, {"type": "response.function_call_arguments.delta", "delta": '{"query":'})
        sse(handler, {"type": "response.function_call_arguments.delta", "delta": '"q"}'})
        sse(handler, {"type": "response.completed", "response": body([
            *body()["output"], {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "PRIVATE"},
            {"type": "function_call", "call_id": "call-1", "name": "search", "arguments": '{"query":"q"}'}])})
        ended.set()
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=replace(config(), base_url=url))
        stream = provider.stream_with_tools([], [])
        assert next(stream) == ("delta", {"text": "answer"})
        assert not ended.is_set()
        release.set()
        events = list(stream)
    assert [name for name, _ in events] == ["complete"]
    parsed = events[-1][1]
    assert parsed.tool_calls[0].arguments == {"query": "q"} and parsed.tool_calls[0].id == "call-1"
    assert "PRIVATE" not in repr(parsed) and parsed.usage.prompt_tokens == 100
    followup = provider._payload([{"role": "assistant", "content": parsed.content, **parsed.protocol_fields},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"}])
    assert followup["input"][1]["encrypted_content"] == "PRIVATE"
    assert len([i for i in followup["input"] if i["type"] == "function_call"]) == 1
    assert followup["input"][-1]["type"] == "function_call_output"


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_terminal_failures_preserve_confirmed_usage(status):
    provider = OpenAICompatibleChatProvider(config=config())
    state = ResponseStream(provider)
    assert state.feed({"type": "response.output_text.delta", "delta": "partial"})
    with pytest.raises(LLMProviderError):
        state.feed({"type": f"response.{status}", "response": body(status=status)})
    assert provider.last_usage.prompt_tokens == 100 and provider.last_usage.reasoning_tokens == 20


def test_disconnect_and_invalid_arguments_cannot_execute_tools():
    provider = OpenAICompatibleChatProvider(config=config())
    state = ResponseStream(provider)
    state.feed({"type": "response.function_call_arguments.delta", "delta": "{}"})
    with pytest.raises(LLMProviderError):
        state.check()
    assert provider.last_usage is None
    for args in ("[]", "invalid"):
        with pytest.raises(LLMProviderError):
            result(provider, body([{"type": "function_call", "call_id": "x", "name": "search", "arguments": args}]))
    assert provider.last_usage.completion_tokens == 25


@pytest.mark.parametrize("json_fallback", [False, True])
def test_async_stream_and_json_fallback(monkeypatch, json_fallback):
    original = httpx.AsyncClient
    response = body()
    content = json.dumps(response) if json_fallback else (
        'data: {"type":"response.output_text.delta","delta":"answer"}\n\n'
        f'data: {json.dumps({"type": "response.completed", "response": response})}\n\n')
    def handler(request):
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, content=content, headers={
            "content-type": "application/json" if json_fallback else "text/event-stream"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    provider = OpenAICompatibleChatProvider(config=config())
    async def collect():
        return [text async for text in provider.stream([])]
    assert asyncio.run(collect()) == ["answer"]
    assert provider.last_usage.reasoning_tokens == 20


def test_private_continuation_is_encrypted_and_not_replayed_across_protocols(monkeypatch):
    from app import mcp_client
    monkeypatch.setattr(mcp_client, "encrypt_headers", lambda values: json.dumps(values))
    monkeypatch.setattr(mcp_client, "decrypt_headers", json.loads)
    selected = config()
    message = {"role": "assistant", "content": "answer", OUTPUT_FIELD: [
        {"type": "reasoning", "encrypted_content": "opaque", "summary": []}]}
    row = SimpleNamespace(role="assistant", content="answer",
                          encrypted_continuation=encrypt_continuation(selected, [message]))
    assert history_message(row, selected) == [message]
    assert history_message(row, replace(selected, api_protocol="chat_completions")) == [{"role": "assistant", "content": "answer"}]
    assert history_message(row, replace(selected, connection_id="different")) == [{"role": "assistant", "content": "answer"}]


def test_protocol_validation_usage_and_endpoint():
    assert api_root("https://example.test/v1/responses") == "https://example.test/v1"
    assert endpoint("https://example.test/v1/responses", "responses") == "https://example.test/v1/responses"
    with pytest.raises(HTTPException):
        endpoint("https://example.test/v1/chat/completions", "responses")
    provider = OpenAICompatibleChatProvider(config=config(thinking=select_thinking(effort(), 1)))
    with pytest.raises(LLMConfigurationError):
        provider._payload([])
    parsed = usage({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 11},
                    "output_tokens_details": {"reasoning_tokens": True}})
    assert parsed.cached_input_tokens is None and parsed.reasoning_tokens is None
    assert parsed.measured()["input_tokens"] == 10


@pytest.mark.parametrize("metered", [False, True])
def test_conversation_tools_billing_and_history_use_responses(client, monkeypatch, metered):
    from sqlalchemy import select
    from app import conversations, mcp_client
    from app.models import BillingSnapshot, ChatMessage, ModelBillingSettings, ModelCallUsage, ModelConnection, SiteModelProvider
    from auth_helpers import model_id
    http, db, _ = client
    selected = db.get(SiteModelProvider, UUID(model_id(http)))
    connection = db.get(ModelConnection, selected.connection_id)
    connection.api_protocol = "responses"
    selected.chat_protocol = "responses"
    # Keep this test on the existing price source, exercise both settlement algorithms.
    db.add(ModelBillingSettings(id=1, enabled=metered, shared_pricing_enabled=False))
    db.commit()
    monkeypatch.setattr(mcp_client, "encrypt_headers", lambda value: json.dumps(value))
    monkeypatch.setattr(mcp_client, "decrypt_headers", json.loads)
    requests = []
    def respond(handler, payload):
        requests.append(payload)
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        if len(requests) == 1:
            output = [{"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque-state"},
                {"type": "function_call", "call_id": "call-1", "name": "unknown", "arguments": "{}"}]
        else:
            assert any(item.get("encrypted_content") == "opaque-state" for item in payload["input"])
            assert any(item.get("type") == "function_call_output" and item["call_id"] == "call-1" for item in payload["input"])
            output = body()["output"]
            sse(handler, {"type": "response.output_text.delta", "delta": "answer"})
        sse(handler, {"type": "response.completed", "response": body(output)})
    with upstream(respond) as url:
        def factory(config):
            assert config.api_protocol == "responses"
            return OpenAICompatibleChatProvider(config=replace(config, base_url=url, site_provider_id=None))
        monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", factory)
        conversation = create(http, [])
        events, request = turn(http, conversation["id"])
        assert events[-1][1]["status"] == "completed", events[-1]
        assert [block["type"] for block in events[-1][1]["display_blocks"]] == ["activity", "text"]
        assert "opaque-state" not in json.dumps(events)
        message = db.scalar(select(ChatMessage).where(ChatMessage.request_id == UUID(request["request_id"]),
                                                      ChatMessage.role == "assistant"))
        assert message.encrypted_continuation
        followup, _ = turn(http, conversation["id"], "continue")
        assert followup[-1][1]["status"] == "completed"
    assert len(requests) == 3
    assert all("messages" not in payload and not payload["store"] for payload in requests)
    if metered:
        calls = db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == UUID(request["request_id"]))).all()
        assert len(calls) == 2 and all(c.usage["completion_tokens"] == 25 for c in calls)
    else:
        calls = db.scalars(select(BillingSnapshot).where(BillingSnapshot.request_id == UUID(request["request_id"]),
                                                        BillingSnapshot.event_key.like("model:%"))).all()
        assert len(calls) == 2 and all(c.context["call_usage"]["completion_tokens"] == 25 for c in calls)
