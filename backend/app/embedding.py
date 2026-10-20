import math
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings, get_settings


class EmbeddingConfigurationError(RuntimeError):
    """Raised when the provider is used before it has been configured."""


class EmbeddingProviderError(RuntimeError):
    """Raised when an OpenAI-compatible provider returns an invalid response."""


@dataclass(frozen=True)
class EmbeddingStatus:
    configured: bool
    provider: str | None
    model: str | None
    dimension: int


class OpenAICompatibleEmbeddingProvider:
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

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.embedding_configured:
            raise EmbeddingConfigurationError(
                "Embedding is not configured. Set EMBEDDING_BASE_URL, "
                "EMBEDDING_API_KEY, and EMBEDDING_MODEL."
            )

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
                timeout=self.settings.embedding_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingProviderError(f"Embedding request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise EmbeddingProviderError(
                f"Embedding provider returned HTTP {response.status_code}: {detail}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError("Embedding provider returned invalid JSON") from exc
        return self._parse_embeddings(body, expected_count=len(texts))

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
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
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
