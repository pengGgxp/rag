import httpx
import pytest

from app.config import Settings
from app.llm import (
    LLMConfigurationError,
    OpenAICompatibleChatProvider,
)


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url="https://provider.example/v1",
        llm_api_key="secret",
        llm_model="chat-model",
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
    result = OpenAICompatibleChatProvider(configured_settings()).complete(
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
