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
    provider = OpenAICompatibleEmbeddingProvider(
        Settings(
            _env_file=None,
            embedding_base_url=None,
            embedding_api_key=None,
            embedding_model=None,
        )
    )

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


@pytest.mark.parametrize("count", [0, 1, 20, 21, 40, 114])
def test_embedding_batches_preserve_input_order(monkeypatch, count):
    batches = []

    def fake_post(url, **kwargs):
        inputs = kwargs["json"]["input"]
        batches.append(inputs)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(text), 0, 1]}
            for index, text in reversed(list(enumerate(inputs)))
        ]})

    monkeypatch.setattr(httpx, "post", fake_post)
    inputs = [str(index) for index in range(count)]
    vectors = OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(inputs)
    assert [len(batch) for batch in batches] == [
        min(20, count - offset) for offset in range(0, count, 20)
    ]
    assert [text for batch in batches for text in batch] == inputs
    assert vectors == [[float(index), 0, 1] for index in range(count)]


def test_failed_batch_stops_without_returning_partial_vectors_or_retrying(monkeypatch):
    batches = []

    def fake_post(url, **kwargs):
        inputs = kwargs["json"]["input"]
        batches.append(inputs)
        if len(batches) == 2:
            return httpx.Response(400, json={"error": "invalid input"})
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [0, 0, 1]}
            for index in range(len(inputs))
        ]})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(EmbeddingProviderError, match="HTTP 400"):
        OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["text"] * 114)
    assert [len(batch) for batch in batches] == [20, 20]


@pytest.mark.parametrize("indexes", [[0, 0], [0, True], [0, 2], [0]])
def test_batch_rejects_invalid_or_duplicate_indexes(monkeypatch, indexes):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(
        200, json={"data": [
            {"index": index, "embedding": [0, 0, 1]} for index in indexes
        ]},
    ))
    with pytest.raises(EmbeddingProviderError):
        OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["a", "b"])
