import httpx
import pytest

from app.config import Settings
from app.embedding import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingProvider,
)


def configured_settings() -> Settings:
    return Settings(
        embedding_base_url="https://provider.example/v1",
        embedding_api_key="secret",
        embedding_model="embedding-model",
        embedding_dimension=3,
    )


def test_embedding_provider_uses_openai_compatible_shape(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["json"] = kwargs["json"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.2, 0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    vectors = OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["a", "b"])

    assert captured["url"] == "https://provider.example/v1/embeddings"
    assert captured["json"] == {"model": "embedding-model", "input": ["a", "b"]}
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert vectors == [[0.1, 0.2, 0.3], [0.2, 0.3, 0.4]]


def test_embedding_provider_requires_credentials():
    provider = OpenAICompatibleEmbeddingProvider(Settings())

    with pytest.raises(EmbeddingConfigurationError):
        provider.embed_texts(["text"])


def test_embedding_provider_rejects_wrong_dimension(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        ),
    )

    with pytest.raises(EmbeddingProviderError, match="dimension"):
        OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["text"])
