import httpx
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings


class LLMConfigurationError(RuntimeError):
    """Raised when the chat provider has not been configured."""


class LLMProviderError(RuntimeError):
    """Raised when the chat provider returns an invalid response."""


@dataclass(frozen=True)
class LLMStatus:
    configured: bool
    provider: str | None
    model: str | None


class OpenAICompatibleChatProvider:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def status(self) -> LLMStatus:
        return LLMStatus(
            configured=self.settings.llm_configured,
            provider="openai-compatible" if self.settings.llm_configured else None,
            model=self.settings.llm_model or None,
        )

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        if not self.settings.llm_configured:
            raise LLMConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL."
            )
        url = self._chat_url(self.settings.llm_base_url or "")
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.settings.llm_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"LLM request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:500].replace("\n", " ")
            raise LLMProviderError(
                f"LLM provider returned HTTP {response.status_code}: {detail}"
            )
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise LLMProviderError("LLM provider returned invalid JSON") from exc
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LLMProviderError("LLM response must contain at least one choice")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("LLM response did not contain message content")
        return content.strip()

    @staticmethod
    def _chat_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        return normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"
