import asyncio
import httpx
import pytest

from app.config import Settings
from app.llm import (
    LLMConfigurationError,
    LLMProviderError,
    OpenAICompatibleChatProvider,
    ChatProviderConfig,
)


def configured_settings() -> ChatProviderConfig:
    return ChatProviderConfig(
        base_url="https://provider.example/v1",
        api_key="secret",
        model="chat-model",
    )


def test_chat_provider_uses_openai_compatible_shape(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["json"] = kwargs["json"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Grounded answer"}}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = OpenAICompatibleChatProvider(config=configured_settings()).complete(
        [{"role": "user", "content": "Question"}]
    )

    assert result == "Grounded answer"
    assert captured["url"] == "https://provider.example/v1/chat/completions"
    assert captured["json"]["model"] == "chat-model"
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_chat_provider_requires_configuration():
    provider = OpenAICompatibleChatProvider(
        Settings(
            _env_file=None,
            llm_base_url=None,
            llm_api_key=None,
            llm_model=None,
        )
    )

    with pytest.raises(LLMConfigurationError):
        provider.complete([{"role": "user", "content": "Question"}])


def test_chat_provider_parses_openai_compatible_tool_calls(monkeypatch):
    def fake_post(url, **kwargs):
        assert kwargs["json"]["tools"][0]["function"]["name"] == "search_knowledge_base"
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge_base",
                            "arguments": '{"query":"needle","knowledge_base_ids":["abc"]}',
                        },
                    }],
                },
            }],
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    response = OpenAICompatibleChatProvider(config=configured_settings()).complete_with_tools(
        [{"role": "user", "content": "Question"}],
        [{"type": "function", "function": {
            "name": "search_knowledge_base",
            "parameters": {"type": "object"},
        }}],
    )

    assert response.content is None
    assert response.tool_calls[0].name == "search_knowledge_base"
    assert response.tool_calls[0].arguments["query"] == "needle"


@pytest.mark.parametrize("body,content_type,expected", [
    (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n',
        "text/event-stream", ["你", "好"],
    ),
    ('{"choices":[{"message":{"content":"完整回答"}}]}', "application/json", ["完整回答"]),
])
def test_streaming_and_non_streaming_compatible_provider(monkeypatch, body, content_type, expected):
    captured = {}

    def handle(request):
        import json
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": content_type}, content=body.encode())

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))

    async def collect():
        return [part async for part in OpenAICompatibleChatProvider(config=configured_settings()).stream([{"role": "user", "content": "问题"}])]

    assert asyncio.run(collect()) == expected
    assert captured["stream"] is True
    assert captured["model"] == "chat-model"


@pytest.mark.parametrize("body", [
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n',
    'data: {"error":{"message":"failed"}}\n\n',
    "data: not-json\n\n",
])
def test_stream_errors_are_not_silently_marked_complete(monkeypatch, body):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body,
        )), **kwargs,
    ))

    async def collect():
        return [part async for part in OpenAICompatibleChatProvider(config=configured_settings()).stream([])]

    with pytest.raises(LLMProviderError):
        asyncio.run(collect())
