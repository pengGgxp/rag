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


@pytest.fixture
def retry_clock(monkeypatch):
    from types import SimpleNamespace
    from app import embedding, upstream_retries

    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    monkeypatch.setattr(embedding, "time", clock)
    monkeypatch.setattr(upstream_retries, "time", clock)
    monkeypatch.setattr(upstream_retries.random, "uniform", lambda low, high: (low + high) / 2)
    return clock


def test_ingestion_retries_only_the_rate_limited_batch(monkeypatch, retry_clock):
    from app.embedding import INGESTION_RETRY

    calls, progress = [], []

    def post(_url, *, json, **_):
        inputs = json["input"]
        calls.append((inputs[0], retry_clock.now))
        if inputs[0] == "20" and len(calls) <= 4:
            return httpx.Response(429, headers={"Retry-After": "10"})
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [float(text), 0, 1]}
            for i, text in reversed(list(enumerate(inputs)))
        ]})

    monkeypatch.setattr(httpx, "post", post)
    result = OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(
        list(map(str, range(41))), retry_policy=INGESTION_RETRY, retry_progress=progress.append)
    assert calls == [("0", 0), ("20", 0), ("20", 10.5), ("20", 21), ("20", 31.5), ("40", 31.5)]
    assert progress == [{"number": n, "maximum": 3} for n in (1, 2, 3)] + [None]
    assert [vector[0] for vector in result] == list(range(41))


@pytest.mark.parametrize(("status", "header", "code", "attempts"), [
    (429, "", None, 4), (429, "61", None, 1), (429, "60", None, 2),
    (429, "10", "insufficient_quota", 1), (401, "", None, 1), (400, "", None, 1),
    (503, "", None, 4),
])
def test_ingestion_retry_admission(monkeypatch, retry_clock, status, header, code, attempts):
    from app.embedding import INGESTION_RETRY

    timeouts = []

    def post(_url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return httpx.Response(status, headers={"Retry-After": header}, json={"error": {"code": code}})

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(EmbeddingProviderError):
        OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["a"], retry_policy=INGESTION_RETRY)
    assert len(timeouts) == attempts
    assert all(0 < timeout <= 30 for timeout in timeouts)


@pytest.mark.parametrize(("error", "expected"), [(httpx.ConnectTimeout, 4), (httpx.ReadTimeout, 1)])
def test_ingestion_retries_only_safe_transport_errors(monkeypatch, retry_clock, error, expected):
    from app.embedding import INGESTION_RETRY

    calls = []

    def post(*_, **__):
        calls.append(retry_clock.now)
        raise error("test transport failure")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(EmbeddingProviderError):
        OpenAICompatibleEmbeddingProvider(configured_settings()).embed_texts(["a"], retry_policy=INGESTION_RETRY)
    assert len(calls) == expected
