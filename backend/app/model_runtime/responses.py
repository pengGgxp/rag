"""Stateless Responses adaptation for the existing provider-neutral Agent contract."""
import json
from app.infra.upstream_retries import model_post, model_astream
from copy import deepcopy

from fastapi import HTTPException

SUFFIXES = ("/chat/completions", "/responses")
OUTPUT_FIELD = "_responses_output"


def api_root(url):
    normalized = url.rstrip("/")
    for suffix in SUFFIXES:
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
    return normalized


def endpoint(url, protocol):
    suffix = "/responses" if protocol == "responses" else "/chat/completions"
    normalized = url.rstrip("/")
    if any(normalized.endswith(other) for other in SUFFIXES if other != suffix):
        raise HTTPException(422, "接口地址与调用类型不一致，请填写基础地址或对应接口地址")
    return normalized if normalized.endswith(suffix) else normalized + suffix


def validate_thinking(protocol, thinking):
    if protocol == "chat_completions" and thinking.adapter in ("anthropic", "gemini"):
        raise ValueError("原生思考适配器需要对应的原生对话协议")
    if protocol in ("anthropic", "gemini") and thinking.adapter not in ("none", protocol):
        raise ValueError("原生协议不能使用其他供应商的思考参数")
    if protocol == "responses" and thinking.adapter not in ("none", "openai"):
        raise ValueError("Responses 调用类型仅支持 OpenAI 思考适配器，请调整模型思考配置")


def payload(config, messages, temperature=0.0, *, tools=None, tool_choice="auto", stream=False, resource_context=None):
    from .client import LLMConfigurationError
    try:
        validate_thinking(config.api_protocol, config.thinking)
    except ValueError as exc:
        raise LLMConfigurationError(str(exc)) from None
    if tools and not config.thinking.supports_tools:
        raise LLMConfigurationError("此模型配置不支持工具调用，请选择其他模型")
    from ..conversations.attachments import model_messages
    messages = model_messages(messages, config, resource_context)
    items = []
    for message in messages:
        if message.get("role") == "assistant" and OUTPUT_FIELD in message:
            items.extend(deepcopy(message[OUTPUT_FIELD]))
            continue
        if message.get("role") == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
            continue
        content = message.get("content")
        if content:
            if isinstance(content, list):
                content = [{"type": "input_text", "text": part["text"]} if part["type"] == "text"
                    else {"type": "input_image", "image_url": part["image_url"]["url"], "detail": "auto"}
                    for part in content]
            items.append({"role": message["role"], "content": content})
        for call in message.get("tool_calls") or []:
            items.append({"type": "function_call", "call_id": call["id"],
                "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    result = {"model": config.model, "input": items, "store": False,
              "include": ["reasoning.encrypted_content"]}
    if config.thinking.kind == "effort":
        result["reasoning"] = {"effort": config.thinking.value}
    if config.max_output_tokens:
        result["max_output_tokens"] = config.max_output_tokens
    if tools:
        result["tools"] = []
        for tool in tools:
            if tool.get("type") != "function":
                raise LLMConfigurationError("Responses 当前仅支持函数工具")
            function = deepcopy(tool["function"])
            # Existing MCP schemas may contain optional fields; do not silently enable strict mode.
            function.setdefault("strict", False)
            result["tools"].append({"type": "function", **function})
        result["tool_choice"] = ({"type": "function", "name": tool_choice["function"]["name"]}
                                  if isinstance(tool_choice, dict) else tool_choice)
    if stream:
        result["stream"] = True
    return result


def usage(raw):
    from .client import _parse_usage
    if not isinstance(raw, dict):
        return None
    return _parse_usage({"prompt_tokens": raw.get("input_tokens"),
        "completion_tokens": raw.get("output_tokens"), "total_tokens": raw.get("total_tokens"),
        "prompt_tokens_details": raw.get("input_tokens_details"),
        "completion_tokens_details": raw.get("output_tokens_details")})


def result(provider, body):
    from .client import LLMProviderError, ToolCall, ToolResponse
    provider.last_usage = usage(body.get("usage")) or provider.last_usage
    if body.get("status") != "completed" or not isinstance(body.get("output"), list):
        raise LLMProviderError("模型响应未完整生成，请重试")
    texts, calls, continuation = [], [], []
    try:
        for item in body["output"]:
            kind = item["type"]
            if item.get("status") not in (None, "completed"):
                raise ValueError
            if kind == "message":
                if item.get("role") != "assistant":
                    raise ValueError
                content = []
                for part in item["content"]:
                    if part["type"] == "refusal":
                        raise LLMProviderError("模型未能回答此请求，请调整问题")
                    if part["type"] != "output_text" or not isinstance(part.get("text"), str):
                        raise ValueError
                    texts.append(part["text"])
                    content.append({"type": "output_text", "text": part["text"], "annotations": []})
                continuation.append({**{key: item[key] for key in ("type", "id", "role", "status", "phase") if key in item},
                                     "content": content})
            elif kind == "function_call":
                calls.append(ToolCall.parse(item["call_id"], item["name"], item["arguments"]))
                continuation.append({key: item[key] for key in ("type", "id", "status", "call_id", "name", "arguments")
                                     if key in item})
            elif kind == "reasoning":
                # Encrypted protocol state is replayed only server-side, never exposed as thinking text.
                continuation.append({key: item[key] for key in ("type", "id", "summary", "encrypted_content")
                                     if key in item})
            else:
                raise ValueError
        if len({call.id for call in calls}) != len(calls):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMProviderError("模型返回了无效的 Responses 内容或工具参数") from exc
    return ToolResponse("".join(texts) or None, calls, provider.last_usage,
                        {OUTPUT_FIELD: continuation})


class ResponseStream:
    """One cumulative terminal response; deltas are never added to usage counters."""
    def __init__(self, provider):
        self.provider = provider
        self.finished = False
        self.text = ""
        self.by_output_index = {}

    @staticmethod
    def stage(item):
        return {"commentary": "commentary", "final_answer": "intermediate"}.get(item.get("phase"))

    def feed(self, body):
        from .client import LLMProviderError
        from .sampling import incomplete_response, upstream_error
        kind = body.get("type")
        if self.finished:
            raise LLMProviderError("模型在完成后返回了额外事件")
        if kind == "response.output_text.delta":
            delta = body.get("delta")
            if not isinstance(delta, str):
                raise LLMProviderError("模型正文片段无效")
            index = body.get("output_index")
            if isinstance(index, int) and index >= 0:
                self.by_output_index[index] = self.by_output_index.get(index, "") + delta
                return [("delta", {"text": delta, "item_index": index})] if delta else []
            self.text += delta
            return [("delta", {"text": delta})] if delta else []
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = body.get("response") or {}
            self.provider.last_usage = usage(response.get("usage")) or self.provider.last_usage
            if kind == "response.failed":
                raise upstream_error(response.get("error"))
            if kind != "response.completed":
                raise incomplete_response((response.get("incomplete_details") or {}).get("reason"))
            parsed = result(self.provider, response)
            full = parsed.content or ""
            outputs = parsed.protocol_fields[OUTPUT_FIELD]
            messages = [(index, item) for index, item in enumerate(outputs) if item["type"] == "message"]
            events = []
            if self.text:
                if self.by_output_index or not full.startswith(self.text):
                    raise LLMProviderError("模型最终正文与流式内容不一致")
                extra = full[len(self.text):]
                if extra:
                    events.append(("delta", {"text": extra}))
                if len(messages) == 1 and (stage := self.stage(messages[0][1])):
                    events.append(("text_phase", {"stage": stage}))
            else:
                remaining = dict(self.by_output_index)
                for index, item in messages:
                    text = "".join(part["text"] for part in item["content"])
                    prefix = remaining.pop(index, "")
                    if not text.startswith(prefix):
                        raise LLMProviderError("模型最终正文与流式内容不一致")
                    if extra := text[len(prefix):]:
                        events.append(("delta", {"text": extra, "item_index": index}))
                    if stage := self.stage(item):
                        events.append(("text_phase", {"stage": stage, "item_index": index}))
                if remaining:
                    raise LLMProviderError("模型最终正文与流式内容不一致")
            self.finished = True
            return events + [("complete", parsed)]
        if kind == "error":
            raise upstream_error(body.get("error", body))
        # Argument fragments are deliberately held until the terminal response validates the whole call.
        return []

    def check(self):
        from .sampling import stream_closed
        if not self.finished:
            raise stream_closed()


def stream(provider, messages, tools, tool_choice):
    import httpx
    from httpx_sse import SSEError
    from .client import LLMProviderError
    from .sampling import active_check, model_stream, stream_events, stream_error
    state = ResponseStream(provider)
    provider.stream_diagnostic = "sse"
    url = endpoint(provider.base_url, "responses")
    body = payload(provider.config, messages, tools=tools, tool_choice=tool_choice, stream=True,
                   resource_context=getattr(provider, "resource_context", None))
    socket = getattr(provider, "responses_socket", None) if active_check() else None
    try:
        if socket and not socket.fallback:
            provider.stream_diagnostic = "websocket"
            provider.request_observation = {"attempts": 1, "accepted_response": False}
            try:
                for body_event in socket.events(url, provider.api_key, body):
                    provider.request_observation["accepted_response"] = True
                    for name, data in state.feed(body_event):
                        if name == "complete":
                            socket.record_response(body, body_event["response"].get("id"),
                                                   data.protocol_fields[OUTPUT_FIELD])
                        yield name, data
                state.check()
            except BaseException:
                socket.close()
                raise
            return
        with model_stream(provider, "POST", url,
                headers=socket.headers(provider.api_key) if socket else {"Authorization": f"Bearer {provider.api_key}"},
                json=body,
                timeout=provider.timeout_seconds) as response:
            if socket:
                socket.capture_route(response.headers)
            if response.status_code >= 400:
                raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}），请检查调用类型与模型配置")
            if "application/json" in response.headers.get("content-type", ""):
                provider.stream_diagnostic = "upstream_returned_json"
                parsed = result(provider, json.loads(response.read()))
                if parsed.content:
                    yield "delta", {"text": parsed.content}
                yield "complete", parsed
                return
            for event in stream_events(response):
                if event.data and event.data != "[DONE]":
                    yield from state.feed(event.json())
                    if state.finished:
                        return
            state.check()
    except LLMProviderError:
        raise
    except (httpx.HTTPError, SSEError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise stream_error(exc) from exc


def complete(provider, messages, tools=None, tool_choice="auto", temperature=0.0):
    import httpx
    from .client import LLMProviderError
    try:
        response = model_post(provider, endpoint(provider.base_url, "responses"),
            headers={"Authorization": f"Bearer {provider.api_key}"},
            json=payload(provider.config, messages, temperature, tools=tools, tool_choice=tool_choice),
            timeout=provider.timeout_seconds)
        if response.status_code >= 400:
            raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}），请检查调用类型与模型配置")
        return result(provider, response.json())
    except LLMProviderError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LLMProviderError("模型连接或响应异常，请重试") from exc


async def stream_text(provider, messages):
    import httpx
    from httpx_sse import SSEError
    from .client import LLMProviderError
    state = ResponseStream(provider)
    provider.stream_diagnostic = "sse"
    try:
        async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
            async with model_astream(provider, client, "POST", endpoint(provider.base_url, "responses"),
                    headers={"Authorization": f"Bearer {provider.api_key}"},
                    json=payload(provider.config, messages, stream=True)) as response:
                from httpx_sse import EventSource
                source = EventSource(response)
                if source.response.status_code >= 400:
                    raise LLMProviderError(f"模型请求失败（HTTP {source.response.status_code}）")
                if "application/json" in source.response.headers.get("content-type", ""):
                    provider.stream_diagnostic = "upstream_returned_json"
                    parsed = result(provider, json.loads(await source.response.aread()))
                    if not parsed.content:
                        raise LLMProviderError("模型未返回正文")
                    yield parsed.content
                    return
                emitted_text = False
                async for event in source.aiter_sse():
                    if event.data and event.data != "[DONE]":
                        for name, data in state.feed(event.json()):
                            if name == "delta":
                                emitted_text = True
                                yield data["text"]
                state.check()
                if not emitted_text:
                    raise LLMProviderError("模型未返回正文")
    except LLMProviderError:
        raise
    except (httpx.HTTPError, SSEError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LLMProviderError("模型流式连接或响应异常，请重试") from exc
