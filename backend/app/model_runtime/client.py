import httpx
import json
from collections.abc import AsyncIterator, Iterator
from httpx_sse import SSEError
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.infra.config import Settings, get_settings
from app.model_catalog.thinking import ThinkingSelection
from app.infra.upstream_retries import model_post, model_stream, model_astream


class LLMConfigurationError(RuntimeError):
    """Raised when the chat provider has not been configured."""


class LLMProviderError(RuntimeError):
    """Raised when the chat provider returns an invalid response."""


class LLMContextCapacityError(LLMProviderError):
    """An explicit provider rejection before generation, never a transport guess."""
    code = "provider_context_capacity_exceeded"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: Any = field(default=None, repr=False)
    argument_error: str | None = None

    @classmethod
    def parse(cls, identifier, name, value):
        if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name:
            raise LLMProviderError("模型返回了无效的工具名称或调用标识")
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            if not isinstance(parsed, dict):
                raise ValueError
            return cls(identifier, name, parsed)
        except (ValueError, TypeError):
            return cls(identifier, name, {}, value, "工具参数必须是有效的 JSON 对象，请修正参数后重新调用")

    def serialized_arguments(self):
        value = self.raw_arguments if self.argument_error else self.arguments
        return value if self.argument_error and isinstance(value, str) else json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class ToolResponse:
    content: str | None
    tool_calls: list[ToolCall]
    usage: "LLMUsage | None" = None
    protocol_fields: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    source: str = "provider"
    cached_input_tokens: int | None = None
    cache_status: str = "missing"
    diagnostics: tuple[str, ...] = ()
    reasoning_tokens: int | None = None

    def measured(self) -> dict:
        result = asdict(self)
        result["input_tokens"] = (
            self.prompt_tokens - (self.cached_input_tokens or 0)
            if self.prompt_tokens is not None else None
        )
        return result


def measured_usage(usage, messages, output, tools=None, *, success=True):
    """Estimate only successful responses, never invent cache hits or failed usage."""
    values = usage.measured() if usage else LLMUsage().measured()
    estimated = []
    if success:
        if values["prompt_tokens"] is None:
            values["prompt_tokens"] = estimate_tokens([{"messages": messages, "tools": tools or []}])
            values["input_tokens"] = values["prompt_tokens"]
            estimated.append("input")
        if values["completion_tokens"] is None:
            values["completion_tokens"] = estimate_tokens([output])
            estimated.append("output")
    values["estimated_fields"] = estimated
    values["source"] = ("unavailable" if usage is None and not success else
        "estimate" if usage is None else "mixed" if estimated else "provider")
    return values


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative provider-neutral fallback when usage is omitted."""
    from ..conversations.attachments import estimate_content
    text = json.dumps(estimate_content(messages), ensure_ascii=False)
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class LLMStatus:
    configured: bool
    provider: str | None
    model: str | None
    provider_id: str | None = None
    provider_key: str | None = None


@dataclass(frozen=True)
class ChatProviderConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 60.0
    provider_id: str | None = None
    provider_key: str | None = None
    provider_name: str | None = None
    site_provider_id: str | None = None
    thinking: ThinkingSelection = ThinkingSelection()
    context_capacity: int | None = None
    max_output_tokens: int | None = None
    config_version: int = 0
    connection_id: str | None = None
    connection_version: int = 0
    api_protocol: str = "chat_completions"
    supports_images: bool = False
    responses_websocket: bool = False

    @property
    def continuation_required(self):
        return self.api_protocol in ("responses", "anthropic", "gemini") or self.thinking.continuation_required

    @property
    def llm_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class OpenAICompatibleChatProvider:
    def __init__(self, settings: Settings | None = None, config: ChatProviderConfig | None = None):
        self.settings = settings or get_settings()
        self.config = config
        self.last_usage: LLMUsage | None = None
        self.thinking_characters = 0
        self.stream_diagnostic = None

    def reset_observation(self):
        self.last_usage = None
        self.thinking_characters = 0
        self.stream_diagnostic = None
        self.request_observation = {}

    def thinking_diagnostic(self):
        return {
            "received_thinking": self.thinking_characters > 0,
            "reasoning_tokens": self.last_usage.reasoning_tokens if self.last_usage else None,
            "stream": self.stream_diagnostic or "not_streamed",
            "upstream": getattr(self, "request_observation", {}),
        }

    @property
    def configured(self) -> bool:
        return bool(self.config and self.config.llm_configured)

    @property
    def base_url(self) -> str:
        return self.config.base_url if self.config else ""

    @property
    def api_key(self) -> str:
        return self.config.api_key if self.config else ""

    @property
    def model(self) -> str:
        return self.config.model if self.config else ""

    @property
    def timeout_seconds(self) -> float:
        return self.config.timeout_seconds if self.config else 60.0

    def check_available(self):
        if not self.configured:
            raise LLMConfigurationError("请在问答输入区选择可用模型")
        if self.config.site_provider_id:
            from app.model_catalog.shared_providers import assert_available
            assert_available(self.config.site_provider_id, self.config.connection_id)

    def _payload(self, messages, temperature=0.0, *, tools=None, tool_choice="auto", stream=False):
        if self.config.api_protocol in ("anthropic", "gemini"):
            from .native import request
            return request(self, messages, tools, tool_choice, temperature, stream)[2]
        if self.config.api_protocol == "responses":
            from .responses import payload
            return payload(self.config, messages, temperature, tools=tools, tool_choice=tool_choice, stream=stream,
                resource_context=getattr(self, "resource_context", None))
        thinking = self.config.thinking
        if tools and not thinking.supports_tools:
            raise LLMConfigurationError("此模型配置不支持当前协议的工具调用，请选择其他模型")
        from ..conversations.attachments import model_messages
        payload = {"model": self.model, "messages": model_messages(messages, self.config,
            getattr(self, "resource_context", None))}
        # Older encrypted continuations also contain empty call arrays. Normalize
        # the outbound copy so existing conversations work without rewriting history.
        for message in payload["messages"]:
            if message.get("role") == "assistant" and message.get("tool_calls") == []:
                message.pop("tool_calls")
        if thinking.adapter == "none" or (
            thinking.adapter in ("deepseek", "bailian") and thinking.value == "disabled"
        ):
            payload["temperature"] = temperature
        payload.update(thinking.parameters())
        if self.config.max_output_tokens:
            payload["max_completion_tokens" if thinking.adapter == "openai" else "max_tokens"] = self.config.max_output_tokens
        if tools:
            payload.update(tools=tools, tool_choice=tool_choice)
        if stream:
            payload.update(stream=True, stream_options={"include_usage": True})
        return payload

    def _post_message(self, payload):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            if self.config.thinking.stream_only:
                self.stream_diagnostic = "sse"
                payload.update(stream=True, stream_options={"include_usage": True})
                with model_stream(self, "POST", self._chat_url(self.base_url), headers=headers,
                                  json=payload, timeout=self.timeout_seconds) as response:
                    if response.status_code >= 400:
                        raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
                    return self._collect_tool_stream(response)
            response = model_post(self, self._chat_url(self.base_url), headers=headers,
                                  json=payload, timeout=self.timeout_seconds)
            if response.status_code >= 400:
                raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
            body = response.json()
        except httpx.HTTPError as exc:
            raise LLMProviderError("模型连接失败，请重试") from exc
        except ValueError as exc:
            raise LLMProviderError("模型返回无效 JSON") from exc
        except (KeyError, TypeError, AttributeError, SSEError) as exc:
            raise LLMProviderError("模型返回无效响应") from exc
        self.last_usage = _parse_usage(body.get("usage") if isinstance(body, dict) else None)
        if isinstance(body, dict):
            for choice in body.get("choices", [])[:1]:
                reasoning = (choice.get("message") or {}).get("reasoning_content")
                if isinstance(reasoning, str):
                    self.thinking_characters += len(reasoning)
        return body

    def _collect_tool_stream(self, response):
        from httpx_sse import EventSource
        message = {"content": "", "reasoning_content": ""}
        calls = {}
        finished = False
        for event in EventSource(response).iter_sse():
            if not event.data:
                continue
            if event.data == "[DONE]":
                break
            body = event.json()
            usage = _parse_usage(body.get("usage"))
            if usage is not None and not (
                self.last_usage is not None and self.last_usage.prompt_tokens is not None
                and self.last_usage.completion_tokens is not None
                and (usage.prompt_tokens is None or usage.completion_tokens is None)
            ):
                self.last_usage = usage
            if body.get("error"):
                raise LLMProviderError("模型流式调用失败")
            for choice in body.get("choices", []):
                delta = choice.get("delta", {})
                for key in ("content", "reasoning_content"):
                    if isinstance(delta.get(key), str):
                        message[key] += delta[key]
                        if key == "reasoning_content":
                            self.thinking_characters += len(delta[key])
                for part in delta.get("tool_calls", []):
                    index = part["index"]
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                     "function": {"name": "", "arguments": ""}})
                    if part.get("id"):
                        call["id"] = part["id"]
                    for key in ("name", "arguments"):
                        call["function"][key] += part.get("function", {}).get(key, "")
                if choice.get("finish_reason") in ("length", "content_filter"):
                    raise LLMProviderError("模型响应未完整生成，请重试")
                if choice.get("finish_reason") in ("stop", "tool_calls"):
                    finished = True
        if not finished:
            raise LLMProviderError("模型响应未完成，请重试")
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return {"choices": [{"message": message}]}

    @property
    def status(self) -> LLMStatus:
        return LLMStatus(
            configured=self.configured,
            provider=(
                self.config.provider_name if self.config else "openai-compatible"
            ) if self.configured else None,
            model=self.model or None,
            provider_id=self.config.provider_id if self.config else None,
            provider_key=self.config.provider_key if self.config else None,
        )

    def complete(self, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
        self.reset_observation()
        self.check_available()
        if self.config.api_protocol in ("anthropic", "gemini"):
            from .native import complete
            response = complete(self, messages, temperature=temperature)
            if not response.content:
                raise LLMProviderError("模型未返回正文")
            return response.content
        if self.config.api_protocol == "responses":
            from .responses import complete
            response = complete(self, messages, temperature=temperature)
            if not response.content:
                raise LLMProviderError("模型未返回正文")
            return response.content.strip()
        if not self.configured:
            raise LLMConfigurationError(
                "请在问答输入区选择可用模型"
            )
        body = self._post_message(self._payload(messages, temperature))
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
        self.reset_observation()
        self.check_available()
        if self.config.api_protocol in ("anthropic", "gemini"):
            from .native import complete
            return complete(self, messages, tools, tool_choice)
        if self.config.api_protocol == "responses":
            from .responses import complete
            return complete(self, messages, tools, tool_choice)
        if not self.configured:
            raise LLMConfigurationError(
                "回答模型尚未配置，请在工作区设置中选择模型"
            )
        body = self._post_message(self._payload(messages, tools=tools, tool_choice=tool_choice))
        return self._tool_response(body)

    def _tool_response(self, body) -> ToolResponse:
        try:
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM response did not contain a valid tool message") from exc
        raw_calls = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for raw in raw_calls:
            try:
                function = raw["function"]
                calls.append(ToolCall.parse(raw.get("id"), function["name"], function.get("arguments", {})))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LLMProviderError("LLM returned an invalid tool call") from exc
        if len({call.id for call in calls}) != len(calls):
            raise LLMProviderError("模型返回了重复的工具调用标识")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise LLMProviderError("LLM returned invalid tool-call content")
        usage = self.last_usage
        return ToolResponse(
            content=content.strip() if isinstance(content, str) else None,
            tool_calls=calls,
            usage=usage,
            protocol_fields=({"reasoning_content": message["reasoning_content"]}
                if self.config.thinking.continuation_required
                and isinstance(message.get("reasoning_content"), str)
                and message["reasoning_content"] else None),
        )

    def stream_with_tools(self, messages, tools, *, tool_choice="auto") -> Iterator[tuple[str, Any]]:
        """Forward public text as received; assemble private protocol fields and tool JSON server-side."""
        from .sampling import incomplete_response, model_stream, stream_events, stream_closed, stream_error, upstream_error
        self.reset_observation()
        self.check_available()
        if self.config.api_protocol in ("anthropic", "gemini"):
            from .native import stream
            yield from stream(self, messages, tools, tool_choice)
            return
        if self.config.api_protocol == "responses":
            from .responses import stream
            yield from stream(self, messages, tools, tool_choice)
            return
        self.stream_diagnostic = "sse"
        message = {"content": "", "reasoning_content": ""}
        calls = {}
        finished = False
        finish_error = None
        try:
            with model_stream(self, "POST", self._chat_url(self.base_url),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=self._payload(messages, tools=tools, tool_choice=tool_choice, stream=True),
                    timeout=self.timeout_seconds) as response:
                if response.status_code >= 400:
                    raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
                if "application/json" in response.headers.get("content-type", ""):
                    body = json.loads(response.read())
                    self.last_usage = _parse_usage(body.get("usage"))
                    self.stream_diagnostic = "upstream_returned_json"
                    raw_message = (body.get("choices") or [{}])[0].get("message") or {}
                    reasoning = raw_message.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        self.thinking_characters += len(reasoning)
                        if self.config.thinking.displays_reasoning:
                            yield "thinking_delta", {"text": reasoning}
                    result = self._tool_response(body)
                    if result.content:
                        if self.thinking_characters and self.config.thinking.displays_reasoning:
                            yield "thinking_status", {"status": "completed"}
                        yield "delta", {"text": result.content}
                    elif self.thinking_characters and self.config.thinking.displays_reasoning:
                        yield "thinking_status", {"status": "completed"}
                    yield "complete", result
                    return
                for event in stream_events(response):
                    if not event.data:
                        continue
                    if event.data == "[DONE]":
                        break
                    body = event.json()
                    usage = _parse_usage(body.get("usage"))
                    if usage is not None:
                        if (self.last_usage and self.last_usage.prompt_tokens is not None
                                and self.last_usage.completion_tokens is not None
                                and (usage.prompt_tokens is None or usage.completion_tokens is None)):
                            self.last_usage = replace(self.last_usage, diagnostics=(
                                *self.last_usage.diagnostics, "ignored_incomplete_stream_usage"))
                        else:
                            self.last_usage = usage
                    if body.get("error"):
                        raise upstream_error(body["error"])
                    for choice in body.get("choices", [])[:1]:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str):
                            message["reasoning_content"] += reasoning
                            self.thinking_characters += len(reasoning)
                            if reasoning and self.config.thinking.displays_reasoning:
                                yield "thinking_delta", {"text": reasoning}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            if self.thinking_characters and not message["content"] and self.config.thinking.displays_reasoning:
                                yield "thinking_status", {"status": "completed"}
                            message["content"] += text
                            yield "delta", {"text": text}
                        for part in delta.get("tool_calls", []):
                            index = part.get("index")
                            if type(index) is not int or index < 0 or index > 1024:
                                raise LLMProviderError("工具调用序号无效")
                            call = calls.setdefault(index, {"id": "", "type": "function",
                                "function": {"name": "", "arguments": ""}})
                            if part.get("id"):
                                call["id"] = part["id"]
                            for key in ("name", "arguments"):
                                fragment = (part.get("function") or {}).get(key, "")
                                if not isinstance(fragment, str):
                                    raise LLMProviderError("工具参数片段无效")
                                call["function"][key] += fragment
                        reason = choice.get("finish_reason")
                        if reason in ("length", "content_filter"):
                            # Usage may arrive after the terminal choice in a separate frame.
                            finish_error = incomplete_response(reason)
                        if reason in ("stop", "tool_calls"):
                            finished = True
                if finish_error:
                    raise finish_error
                if not finished:
                    raise stream_closed()
                message["tool_calls"] = [calls[index] for index in sorted(calls)]
                if self.thinking_characters and self.config.thinking.displays_reasoning:
                    yield "thinking_status", {"status": "completed"}
                yield "complete", self._tool_response({"choices": [{"message": message}]})
        except LLMProviderError:
            raise
        except (httpx.HTTPError, SSEError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise stream_error(exc) from exc

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        self.reset_observation()
        self.check_available()
        if self.config.api_protocol in ("anthropic", "gemini"):
            from .native import stream_text
            async for text in stream_text(self, messages):
                yield text
            return
        if self.config.api_protocol == "responses":
            from .responses import stream_text
            async for text in stream_text(self, messages):
                yield text
            return
        self.stream_diagnostic = "sse"
        if not self.configured:
            raise LLMConfigurationError("回答模型尚未配置")
        payload = self._payload(messages, stream=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with model_astream(
                    self, client, "POST", self._chat_url(self.base_url),
                    headers=headers, json=payload,
                ) as response:
                    from httpx_sse import EventSource
                    source = EventSource(response)
                    if source.response.status_code >= 400:
                        raise LLMProviderError(f"回答服务请求失败（HTTP {source.response.status_code}）")
                    # Some compatible providers ignore stream=True and return one JSON response.
                    if "application/json" in source.response.headers.get("content-type", ""):
                        body = json.loads(await source.response.aread())
                        self.stream_diagnostic = "upstream_returned_json"
                        self.last_usage = _parse_usage(body.get("usage"))
                        reasoning = body["choices"][0]["message"].get("reasoning_content")
                        if isinstance(reasoning, str):
                            self.thinking_characters += len(reasoning)
                        content = body["choices"][0]["message"]["content"]
                        if not isinstance(content, str) or not content.strip():
                            raise LLMProviderError("回答服务未返回正文")
                        yield content
                        return
                    received = False
                    finished = False
                    async for event in source.aiter_sse():
                        if not event.data:
                            continue
                        if event.data == "[DONE]":
                            finished = True
                            break
                        body = event.json()
                        parsed_usage = _parse_usage(body.get("usage"))
                        if parsed_usage is not None:
                            if (self.last_usage is not None
                                    and self.last_usage.prompt_tokens is not None
                                    and self.last_usage.completion_tokens is not None
                                    and (parsed_usage.prompt_tokens is None or parsed_usage.completion_tokens is None)):
                                self.last_usage = replace(self.last_usage, diagnostics=(
                                    *self.last_usage.diagnostics, "ignored_incomplete_stream_usage"))
                            else:
                                self.last_usage = parsed_usage
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
                        reasoning = choice.get("delta", {}).get("reasoning_content")
                        if isinstance(reasoning, str):
                            self.thinking_characters += len(reasoning)
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


def _parse_usage(raw: Any) -> LLMUsage | None:
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    total = raw.get("total_tokens")
    values = [value if type(value) is int and value >= 0 else None for value in (prompt, completion, total)]
    if all(value is None for value in values):
        return None
    details = raw.get("prompt_tokens_details")
    candidates = []
    if isinstance(details, dict) and "cached_tokens" in details:
        candidates.append(details["cached_tokens"])
    if "prompt_cache_hit_tokens" in raw:
        candidates.append(raw["prompt_cache_hit_tokens"])
    diagnostics = []
    cached = None
    status = "missing"
    if candidates:
        if (any(type(value) is not int or value < 0 for value in candidates)
                or len(set(candidates)) != 1 or values[0] is None
                or candidates[0] > values[0]):
            status = "invalid"
            diagnostics.append("invalid_or_conflicting_cache_usage")
        else:
            cached = candidates[0]
            status = "reported"
    if any(raw.get(key) is not None and value is None for key, value in zip(
            ("prompt_tokens", "completion_tokens", "total_tokens"), values)):
        diagnostics.append("invalid_token_count")
    output_details = raw.get("completion_tokens_details")
    reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
    if reasoning is not None and (
        type(reasoning) is not int or reasoning < 0 or (values[1] is not None and reasoning > values[1])
    ):
        diagnostics.append("invalid_reasoning_token_count")
        reasoning = None
    return LLMUsage(prompt_tokens=values[0], completion_tokens=values[1], total_tokens=values[2],
                    cached_input_tokens=cached, cache_status=status, diagnostics=tuple(diagnostics),
                    reasoning_tokens=reasoning)
