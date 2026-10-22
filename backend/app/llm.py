import httpx
import json
from collections.abc import AsyncIterator
from httpx_sse import aconnect_sse, SSEError
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings


class LLMConfigurationError(RuntimeError):
    """Raised when the chat provider has not been configured."""


class LLMProviderError(RuntimeError):
    """Raised when the chat provider returns an invalid response."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResponse:
    content: str | None
    tool_calls: list[ToolCall]


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

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> ToolResponse:
        """Run one OpenAI-compatible tool-calling turn.

        This deliberately returns a small provider-neutral shape so the Agent
        runtime does not depend on a vendor SDK or on provider-specific
        message classes.
        """
        if not self.settings.llm_configured:
            raise LLMConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL."
            )
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.0,
        }
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice)
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                self._chat_url(self.settings.llm_base_url or ""),
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
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM response did not contain a valid tool message") from exc
        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for raw in raw_calls:
            try:
                function = raw["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise TypeError
                calls.append(ToolCall(
                    id=str(raw.get("id") or f"tool_{len(calls) + 1}"),
                    name=str(function["name"]),
                    arguments=arguments,
                ))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LLMProviderError("LLM returned an invalid tool call") from exc
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise LLMProviderError("LLM returned invalid tool-call content")
        return ToolResponse(content=content.strip() if isinstance(content, str) else None, tool_calls=calls)

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        if not self.settings.llm_configured:
            raise LLMConfigurationError("回答模型尚未配置")
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.0,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                async with aconnect_sse(
                    client, "POST", self._chat_url(self.settings.llm_base_url or ""),
                    headers=headers, json=payload,
                ) as source:
                    if source.response.status_code >= 400:
                        raise LLMProviderError(f"回答服务请求失败（HTTP {source.response.status_code}）")
                    # Some compatible providers ignore stream=True and return one JSON response.
                    if "application/json" in source.response.headers.get("content-type", ""):
                        body = json.loads(await source.response.aread())
                        content = body["choices"][0]["message"]["content"]
                        if not isinstance(content, str) or not content.strip():
                            raise LLMProviderError("回答服务未返回正文")
                        yield content
                        return
                    received = False
                    finished = False
                    async for event in source.aiter_sse():
                        if event.data == "[DONE]":
                            finished = True
                            break
                        body = event.json()
                        if body.get("error"):
                            raise LLMProviderError("回答服务返回错误")
                        choices = body.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        reason = choice.get("finish_reason")
                        if reason in ("length", "content_filter"):
                            raise LLMProviderError("回答未完整生成，请重试")
                        if reason is not None:
                            finished = True
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str) and content:
                            received = True
                            yield content
                    if not received or not finished:
                        raise LLMProviderError("回答连接中断，未收到完整回答")
        except LLMProviderError:
            raise
        except (httpx.HTTPError, SSEError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise LLMProviderError("回答服务连接或响应异常，请重试") from exc

    @staticmethod
    def _chat_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        return normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"
