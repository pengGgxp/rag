import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.infra.config import Settings, get_settings
from app.infra.upstream_retries import RetryBudget, RetryPolicy, TRANSIENT_STATUS, retry_after


INGESTION_RETRY = RetryPolicy(
    attempts=4, base_delay=2, max_delay=60, seconds=120, retry_after_jitter=1, check_interval=1,
)
INGESTION_WAIT_SECONDS = 120


class EmbeddingConfigurationError(RuntimeError):
    """Raised when the provider is used before it has been configured."""


class EmbeddingProviderError(RuntimeError):
    """Raised when an OpenAI-compatible provider returns an invalid response."""

    def __init__(self, message: str, *, status: int | None = None, permanent_quota: bool = False,
                 retry_after_header: str | None = None, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent_quota = permanent_quota
        self.retry_after_header = retry_after_header
        self.uncertain = uncertain

    @property
    def user_message(self) -> str:
        if self.permanent_quota or self.status in (401, 403, 404):
            return "文档处理服务不可用，请联系管理员"
        if self.status in (400, 413, 422):
            return "文档内容无法处理，请检查文件或联系管理员"
        if self.status == 429:
            return "处理服务暂时繁忙，请稍后重试"
        return "文档处理未完成，请稍后重试；若仍失败，请联系管理员"


@dataclass(frozen=True)
class EmbeddingStatus:
    configured: bool
    provider: str | None
    model: str | None
    dimension: int


class OpenAICompatibleEmbeddingProvider:
    # A conservative limit also accepted by the configured DashScope endpoint.
    MAX_BATCH_SIZE = 20

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def status(self) -> EmbeddingStatus:
        return EmbeddingStatus(
            configured=self.settings.embedding_configured,
            provider="openai-compatible" if self.settings.embedding_configured else None,
            model=self.settings.embedding_model or None,
            dimension=self.settings.embedding_dimension,
        )

    def embed_texts(self, texts: list[str], *, retry_policy: RetryPolicy | None = None,
                    retry_progress=None, batch_start=None, batch_rejected=None,
                    batch_success=None) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.embedding_configured:
            raise EmbeddingConfigurationError(
                "Embedding is not configured. Set EMBEDDING_BASE_URL, "
                "EMBEDDING_API_KEY, and EMBEDDING_MODEL."
            )

        vectors = []
        wait_remaining = INGESTION_WAIT_SECONDS
        for offset in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[offset:offset + self.MAX_BATCH_SIZE]
            if retry_policy is None:
                result = self._embed_batch(batch)
            else:
                result, waited = self._retry_batch(
                    batch, retry_policy, wait_remaining, retry_progress, batch_start, batch_rejected)
                wait_remaining = max(0, wait_remaining - waited)
            vectors.extend(result)
            if batch_success:
                batch_success(offset, result)
        return vectors

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed one provider-sized batch without layering another retry policy."""
        if not self.settings.embedding_configured:
            raise EmbeddingConfigurationError(
                "Embedding is not configured. Set EMBEDDING_BASE_URL, "
                "EMBEDDING_API_KEY, and EMBEDDING_MODEL."
            )
        if not texts:
            return []
        if len(texts) > self.MAX_BATCH_SIZE:
            raise ValueError(f"Embedding batch exceeds {self.MAX_BATCH_SIZE} items")
        return self._embed_batch(texts)

    def _retry_batch(self, texts, policy, wait_remaining, progress, batch_start, batch_rejected):
        budget = RetryBudget("embedding", policy=policy)
        waited = 0.0
        failure = EmbeddingProviderError("Embedding retry budget expired")
        while budget.begin():
            after = None
            remaining = budget.remaining()
            if remaining <= 0:
                break
            if batch_start:
                batch_start()
            try:
                vectors = self._embed_batch(texts, timeout=min(
                    self.settings.embedding_timeout_seconds, remaining))
            except EmbeddingProviderError as error:
                if batch_rejected and not error.uncertain:
                    batch_rejected()
                if error.uncertain:
                    raise
                safe_connection = isinstance(error.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                if not safe_connection:
                    if error.status not in TRANSIENT_STATUS or error.permanent_quota:
                        raise
                    after = retry_after(error.retry_after_header, reject_negative=True)
                delay = budget.delay(f"http_{error.status}" if error.status else "connection_not_sent",
                                     after, remaining_wait=wait_remaining - waited)
                if delay is None:
                    raise
                failure = error
            else:
                if budget.attempt > 1 and progress:
                    progress(None)
                return vectors, waited
            if progress:
                progress({"number": budget.attempt, "maximum": policy.attempts - 1})
            started = time.monotonic()
            budget.wait(delay)
            waited += time.monotonic() - started
        raise failure

    def _embed_batch(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
        url = self._embeddings_url(self.settings.embedding_base_url or "")
        headers = {
            "Authorization": f"Bearer {self.settings.embedding_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.settings.embedding_model, "input": texts}
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.settings.embedding_timeout_seconds if timeout is None else timeout,
            )
        except httpx.HTTPError as exc:
            safe_connection = isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            )
            raise EmbeddingProviderError(
                f"Embedding request {'connection failed' if safe_connection else 'outcome is unknown'}"
                + (f": {type(exc).__name__}" if safe_connection else ""),
                uncertain=not safe_connection,
            ) from exc

        if response.status_code >= 400:
            permanent_quota = False
            if response.status_code == 429:
                try:
                    body = response.json()
                    error = body.get("error") if isinstance(body, dict) else None
                    permanent_quota = isinstance(error, dict) and any(
                        error.get(key) == "insufficient_quota" for key in ("code", "type"))
                except ValueError:
                    pass
            raise EmbeddingProviderError(
                f"Embedding provider returned HTTP {response.status_code}",
                status=response.status_code, permanent_quota=permanent_quota,
                retry_after_header=response.headers.get("Retry-After"),
                uncertain=response.status_code >= 500,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError("Embedding provider returned invalid JSON", uncertain=True) from exc
        try:
            return self._parse_embeddings(body, expected_count=len(texts))
        except EmbeddingProviderError as exc:
            exc.uncertain = True
            raise

    def _embeddings_url(self, base_url: str) -> str:
        normalized = base_url.rstrip("/")
        return normalized if normalized.endswith("/embeddings") else f"{normalized}/embeddings"

    def _parse_embeddings(self, body: Any, expected_count: int) -> list[list[float]]:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingProviderError(
                f"Embedding response must contain {expected_count} data items"
            )

        vectors: list[list[float] | None] = [None] * expected_count
        for item in data:
            if not isinstance(item, dict) or type(item.get("index")) is not int:
                raise EmbeddingProviderError("Embedding response item is missing an integer index")
            index = item["index"]
            values = item.get("embedding")
            if index < 0 or index >= expected_count or not isinstance(values, list):
                raise EmbeddingProviderError("Embedding response contains an invalid item")
            if len(values) != self.settings.embedding_dimension:
                raise EmbeddingProviderError(
                    f"Expected embedding dimension {self.settings.embedding_dimension}, got {len(values)}"
                )
            try:
                vector = [float(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise EmbeddingProviderError("Embedding vector contains a non-numeric value") from exc
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingProviderError("Embedding vector contains a non-finite value")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise EmbeddingProviderError("Embedding response contains duplicate or missing indexes")
        return [vector for vector in vectors if vector is not None]
