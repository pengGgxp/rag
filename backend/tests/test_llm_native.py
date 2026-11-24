import base64
import hashlib
import json
from dataclasses import replace

import pytest

from app.model_runtime.client import ChatProviderConfig, LLMProviderError, OpenAICompatibleChatProvider
from app.model_runtime.native import NativeStream, request, result
from app.model_catalog.thinking import select_thinking
from test_chat_attachments import PNG


def provider(protocol):
    return OpenAICompatibleChatProvider(config=ChatProviderConfig("https://vendor.example/v1", "private-key", "model", api_protocol=protocol))


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_tool_continuation_preserves_native_signatures(protocol):
    p = provider(protocol)
    native = ([{"type": "thinking", "thinking": "private", "signature": "signed"},
               {"type": "tool_use", "id": "a", "name": "lookup", "input": {"q": "x"}}]
              if protocol == "anthropic" else [{"functionCall": {"name": "lookup", "args": {"q": "x"}}, "thoughtSignature": "signed"}])
    body = ({"content": native, "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 4}}
            if protocol == "anthropic" else {"candidates": [{"content": {"parts": native}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 14, "candidatesTokenCount": 2, "thoughtsTokenCount": 1, "cachedContentTokenCount": 4}})
    response = result(p, body)
    call = response.tool_calls[0]
    _, headers, outbound = request(p, [{"role": "system", "content": "system"}, {"role": "user", "content": "lookup"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}],
         **response.protocol_fields}, {"role": "tool", "tool_call_id": call.id, "content": "found"}])
    assert "signed" in json.dumps(outbound)
    assert "private-key" not in json.dumps(outbound)
    assert p.last_usage.measured()["input_tokens"] == 10
    assert p.last_usage.completion_tokens == 3


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_read_image_is_embedded_in_the_matching_native_tool_result(protocol):
    p = provider(protocol)
    p.config = replace(p.config, supports_images=True)
    identifier = "image-call"
    native = ([{"type": "thinking", "thinking": "private", "signature": "signed"},
               {"type": "tool_use", "id": identifier, "name": "read_image", "input": {"path": "chart.png"}}]
              if protocol == "anthropic" else [{"functionCall": {"name": "read_image", "args": {"path": "chart.png"}},
                  "thoughtSignature": "signed"}])
    image = {"type": "model_image", "content_type": "image/png",
        "sha256": hashlib.sha256(PNG).hexdigest(), "data": PNG}
    messages = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": identifier, "function": {"name": "read_image", "arguments": '{"path":"chart.png"}'}}],
         "_native_content": native},
        {"role": "tool", "tool_call_id": identifier, "content": '{"filename":"chart.png"}',
         "_image_input": image},
    ]

    _, _, outbound = request(p, messages, tools=[])
    encoded = base64.b64encode(PNG).decode("ascii")
    serialized = json.dumps(outbound)
    assert "signed" in serialized
    assert encoded in serialized
    assert "private-key" not in serialized
    if protocol == "anthropic":
        tool_result = outbound["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == identifier
        assert tool_result["content"][-1] == {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": encoded}}
    else:
        parts = outbound["contents"][-1]["parts"]
        assert parts[0]["functionResponse"]["name"] == "read_image"
        assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": encoded}}


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_native_tool_image_payload_rejects_tampered_bytes(protocol):
    p = provider(protocol)
    image = {"type": "model_image", "content_type": "image/png", "sha256": "0" * 64, "data": PNG}
    messages = [{"role": "assistant", "content": None,
        "tool_calls": [{"id": "image", "function": {"name": "read_image", "arguments": "{}"}}],
        "_native_content": ([{"type": "tool_use", "id": "image", "name": "read_image", "input": {}}]
            if protocol == "anthropic" else [{"functionCall": {"name": "read_image", "args": {}}}])},
        {"role": "tool", "tool_call_id": "image", "content": "{}", "_image_input": image}]
    with pytest.raises(Exception, match="校验"):
        request(p, messages, tools=[])


def test_anthropic_stream_assembles_fragmented_json_and_signature():
    p = provider("anthropic")
    state = NativeStream(p)
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "a", "name": "lookup", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}, {"type": "message_stop"}]
    for event in events:
        list(state.feed(event))
    assert state.finish().tool_calls[0].arguments == {"q": "x"}
    assert p.last_usage.prompt_tokens == 5


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_truncated_stream_is_never_success(protocol):
    with pytest.raises(LLMProviderError):
        NativeStream(provider(protocol)).finish()


def test_remote_image_rejected_before_native_request():
    p = provider("gemini")
    p.config = replace(p.config, supports_images=True)
    with pytest.raises(Exception, match="已鉴权"):
        request(p, [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://127.0.0.1/secret"}}]}])


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_native_thinking_budget_uses_native_parameters(protocol):
    p = provider(protocol)
    thinking = select_thinking({"kind": "budget", "adapter": protocol,
        "options": [{"id": "standard", "label": "Standard", "value": 2048}], "default_option_id": "standard"}, 1)
    p.config = replace(p.config, thinking=thinking, max_output_tokens=4096)
    _, _, body = request(p, [{"role": "user", "content": "think"}])
    assert "enable_thinking" not in body
    if protocol == "anthropic":
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert "temperature" not in body
    else:
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 2048, "includeThoughts": True}


def test_async_native_stream_closes_transport_when_cancelled(monkeypatch):
    import asyncio
    import httpx
    from app.model_runtime import native as llm_native
    closed = []
    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}\n\n'
            await asyncio.sleep(60)
        async def aclose(self):
            closed.append(True)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(llm_native.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Wire())), **kwargs))
    async def consume():
        stream = llm_native.stream_text(provider("gemini"), [{"role": "user", "content": "hello"}])
        assert await anext(stream) == "hello"
        await stream.aclose()
    asyncio.run(consume())
    assert closed == [True]
