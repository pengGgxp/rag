"""Native Messages/GenerateContent adapters for the Agent's normalized contract."""
import base64
import hashlib
import json
from copy import deepcopy
from uuid import uuid4
from urllib.parse import quote

import httpx
from httpx_sse import EventSource

from .llm import LLMConfigurationError, LLMProviderError, LLMUsage, ToolCall, ToolResponse
from .upstream_retries import model_post, model_stream, model_astream


def _tool_result_image(image, native):
    from .chat_attachments import MAX_IMAGE_BYTES

    if not isinstance(image, dict) or image.get("type") != "model_image":
        return None
    data = image.get("data")
    content_type = image.get("content_type")
    if (not isinstance(data, bytes) or len(data) > MAX_IMAGE_BYTES
            or content_type not in ("image/png", "image/jpeg", "image/webp")
            or hashlib.sha256(data).hexdigest() != image.get("sha256")):
        raise LLMConfigurationError("按需读取的图片校验失败，请重新读取")
    encoded = base64.b64encode(data).decode("ascii")
    if native == "anthropic":
        return {"type": "image", "source": {
            "type": "base64", "media_type": content_type, "data": encoded}}
    return {"inlineData": {"mimeType": content_type, "data": encoded}}


def request(provider, messages, tools=None, tool_choice="auto", temperature=0.0, streaming=False):
    from .chat_attachments import model_messages
    config = provider.config
    if tools and not config.thinking.supports_tools:
        raise LLMConfigurationError("此模型配置不支持工具调用")
    native = config.api_protocol
    messages = model_messages(messages, config, getattr(provider, "resource_context", None))
    system, contents, names = [], [], {}
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(message.get("content") or "")
            continue
        saved = message.get("_native_content")
        if saved and role == "assistant":
            parts = deepcopy(saved)
            for call in message.get("tool_calls", []):
                names[call["id"]] = call["function"]["name"]
        elif role == "tool":
            identifier = message["tool_call_id"]
            image_part = _tool_result_image(message.get("_image_input"), native)
            if native == "anthropic":
                content = message.get("content") or ""
                if image_part:
                    content = [{"type": "text", "text": content}, image_part]
                parts = [{"type": "tool_result", "tool_use_id": identifier, "content": content}]
            else:
                if identifier not in names:
                    raise LLMConfigurationError("工具续接缺少原始调用，请新建对话")
                parts = [{"functionResponse": {"name": names[identifier], "response": {"result": message.get("content") or ""}}}]
                if image_part:
                    parts.append(image_part)
        else:
            parts = []
            raw = message.get("content")
            for part in raw if isinstance(raw, list) else [{"type": "text", "text": raw or ""}]:
                if part["type"] == "text":
                    if part.get("text"):
                        parts.append({"type": "text", "text": part["text"]} if native == "anthropic" else {"text": part["text"]})
                elif part["type"] == "image_url":
                    url = part["image_url"]["url"]
                    if not url.startswith("data:") or ";base64," not in url:
                        raise LLMConfigurationError("原生协议仅接受已鉴权的图片资源")
                    mime, data = url[5:].split(";base64,", 1)
                    base64.b64decode(data, validate=True)
                    parts.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                                 if native == "anthropic" else {"inlineData": {"mimeType": mime, "data": data}})
                else:
                    raise LLMConfigurationError("当前协议不支持此消息内容")
            for call in message.get("tool_calls", []):
                function = call["function"]
                names[call["id"]] = function["name"]
                args = json.loads(function["arguments"])
                parts.append({"type": "tool_use", "id": call["id"], "name": function["name"], "input": args}
                             if native == "anthropic" else {"functionCall": {"name": function["name"], "args": args}})
        if not parts:
            continue
        native_role = "assistant" if native == "anthropic" and role == "assistant" else "model" if role == "assistant" else "user"
        field = "content" if native == "anthropic" else "parts"
        if contents and contents[-1]["role"] == native_role:
            contents[-1][field].extend(parts)
        else:
            contents.append({"role": native_role, field: parts})
    root = provider.base_url.rstrip("/")
    if native == "anthropic":
        url = root if root.endswith("/messages") else root + "/messages"
        body = {"model": provider.model, "messages": contents, "max_tokens": config.max_output_tokens or 4096,
                "stream": streaming, "temperature": temperature}
        if system:
            body["system"] = "\n\n".join(system)
        thinking = config.thinking
        if thinking.adapter == "anthropic":
            if thinking.value == "disabled":
                body["thinking"] = {"type": "disabled"}
            else:
                budget = thinking.value if thinking.kind == "budget" else 1024
                if budget < 1024 or budget >= body["max_tokens"]:
                    raise LLMConfigurationError("思考预算须至少 1024 Token 且小于最大输出")
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
                body.pop("temperature", None)
        if tools:
            if body.get("thinking", {}).get("type") == "enabled" and tool_choice not in ("auto", "none"):
                raise LLMConfigurationError("启用思考时不支持强制工具调用")
            body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                              "input_schema": t["function"]["parameters"]} for t in tools]
            body["tool_choice"] = {"type": "auto" if tool_choice == "auto" else "none" if tool_choice == "none" else "any"}
        headers = {"x-api-key": provider.api_key, "anthropic-version": "2023-06-01"}
    else:
        if not root.endswith(("/v1beta", "/v1")):
            root += "/v1beta"
        action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        url = f"{root}/models/{quote(provider.model.removeprefix('models/'), safe='')}:{action}"
        body = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if config.max_output_tokens:
            body["generationConfig"]["maxOutputTokens"] = config.max_output_tokens
        thinking = config.thinking
        if thinking.adapter == "gemini":
            options = {"includeThoughts": thinking.value != "disabled"}
            if thinking.kind == "budget":
                options["thinkingBudget"] = thinking.value
            elif thinking.kind == "effort":
                options["thinkingLevel"] = str(thinking.value).upper()
            elif thinking.value == "disabled":
                options["thinkingBudget"] = 0
            body["generationConfig"]["thinkingConfig"] = options
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [{"name": t["function"]["name"],
                "description": t["function"].get("description", ""), "parametersJsonSchema": t["function"]["parameters"]} for t in tools]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO" if tool_choice == "auto" else "NONE" if tool_choice == "none" else "ANY"}}
        headers = {"x-goog-api-key": provider.api_key}
    return url, headers, body


def observe_usage(provider, body):
    """Keep confirmed cumulative usage even if the accepted response later breaks."""
    native = provider.config.api_protocol
    if native == "anthropic":
        usage = body.get("usage", {})
        if not usage:
            return
        cached = usage.get("cache_read_input_tokens", 0)
        prompt = usage.get("input_tokens")
        provider.last_usage = LLMUsage(prompt_tokens=(prompt + cached + usage.get("cache_creation_input_tokens", 0)) if prompt is not None else None,
            completion_tokens=usage.get("output_tokens"), cached_input_tokens=cached, cache_status="reported")
    else:
        usage = body.get("usageMetadata", {})
        if not usage:
            return
        cached = usage.get("cachedContentTokenCount", 0)
        completion = usage.get("candidatesTokenCount")
        thoughts = usage.get("thoughtsTokenCount", 0)
        provider.last_usage = LLMUsage(prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=completion + thoughts if completion is not None else None,
            cached_input_tokens=cached, cache_status="reported", reasoning_tokens=thoughts)


def result(provider, body):
    native = provider.config.api_protocol
    observe_usage(provider, body)
    if native == "anthropic":
        if body.get("stop_reason") not in ("end_turn", "tool_use", "stop_sequence"):
            raise LLMProviderError("模型响应未完整生成")
        parts = body.get("content", [])
    else:
        candidates = body.get("candidates") or []
        if not candidates or candidates[0].get("finishReason") != "STOP":
            raise LLMProviderError("模型未完成回答，请调整问题或输出限制")
        parts = candidates[0].get("content", {}).get("parts", [])
    texts, calls = [], []
    for part in parts:
        if native == "anthropic" and part.get("type") == "tool_use":
            calls.append(ToolCall(part["id"], part["name"], part["input"]))
        elif "functionCall" in part:
            call = part["functionCall"]
            calls.append(ToolCall(call.get("id") or f"call_{uuid4().hex}", call["name"], call.get("args", {})))
        elif "text" in part and not part.get("thought"):
            texts.append(part["text"])
    if any(not isinstance(c.arguments, dict) for c in calls) or len({c.id for c in calls}) != len(calls):
        raise LLMProviderError("模型返回无效工具调用")
    return ToolResponse("".join(texts) or None, calls, provider.last_usage, {"_native_content": parts})


def complete(provider, messages, tools=None, tool_choice="auto", temperature=0.0):
    url, headers, body = request(provider, messages, tools, tool_choice, temperature)
    try:
        response = model_post(provider, url, headers=headers, json=body, timeout=provider.timeout_seconds)
        if response.status_code >= 400:
            raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
        return result(provider, response.json())
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise LLMProviderError("模型连接或响应异常，请重试") from exc


class NativeStream:
    def __init__(self, provider):
        self.provider = provider
        self.body = {"content": [], "usage": {}}
        self.blocks = {}
        self.gemini_parts = []
        self.finished = False

    def feed(self, event):
        if self.provider.config.api_protocol == "gemini":
            candidates = event.get("candidates", [])
            if candidates:
                candidate = candidates[0]
                self.gemini_parts.extend(candidate.get("content", {}).get("parts", []))
                self.body["candidates"] = [{**candidate, "content": {"parts": self.gemini_parts}}]
                self.finished = candidate.get("finishReason") == "STOP" or self.finished
                for part in candidate.get("content", {}).get("parts", []):
                    if part.get("text"):
                        if part.get("thought"):
                            self.provider.thinking_characters += len(part["text"])
                        yield ("thinking_delta" if part.get("thought") else "delta"), {"text": part["text"]}
            if "usageMetadata" in event:
                self.body.setdefault("usageMetadata", {}).update(event["usageMetadata"])
                observe_usage(self.provider, self.body)
            return
        kind = event.get("type")
        if kind == "error":
            raise LLMProviderError("模型流式返回错误，请重试")
        if kind == "message_start":
            self.body.update(event["message"])
            observe_usage(self.provider, self.body)
        elif kind == "content_block_start":
            self.blocks[event["index"]] = {"part": deepcopy(event["content_block"]), "json": ""}
        elif kind == "content_block_delta":
            block, delta = self.blocks[event["index"]], event["delta"]
            kind = delta["type"]
            if kind == "input_json_delta":
                block["json"] += delta["partial_json"]
            elif kind == "signature_delta":
                block["part"]["signature"] = block["part"].get("signature", "") + delta["signature"]
            elif kind in ("text_delta", "thinking_delta"):
                field = "text" if kind == "text_delta" else "thinking"
                text = delta[field]
                if field == "thinking":
                    self.provider.thinking_characters += len(text)
                block["part"][field] = block["part"].get(field, "") + text
                yield ("delta" if field == "text" else "thinking_delta"), {"text": text}
        elif kind == "message_delta":
            self.body.update(event.get("delta", {}))
            self.body["usage"].update(event.get("usage", {}))
            observe_usage(self.provider, self.body)
        elif kind == "message_stop":
            self.finished = True

    def finish(self):
        if not self.finished:
            raise LLMProviderError("模型流式响应中断，请重试")
        if self.provider.config.api_protocol == "anthropic":
            parts = []
            for _, block in sorted(self.blocks.items()):
                part = block["part"]
                if part["type"] == "tool_use" and block["json"]:
                    part["input"] = json.loads(block["json"])
                parts.append(part)
            self.body["content"] = parts
        return result(self.provider, self.body)


def stream(provider, messages, tools=None, tool_choice="auto"):
    url, headers, body = request(provider, messages, tools, tool_choice, streaming=True)
    state = NativeStream(provider)
    provider.stream_diagnostic = "sse"
    try:
        with model_stream(provider, "POST", url, headers=headers, json=body, timeout=provider.timeout_seconds) as response:
            if response.status_code >= 400:
                raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
            for event in EventSource(response).iter_sse():
                if event.data:
                    yield from state.feed(json.loads(event.data))
        yield "complete", state.finish()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise LLMProviderError("模型流式连接或响应异常，请重试") from exc


async def stream_text(provider, messages):
    url, headers, body = request(provider, messages, streaming=True)
    state = NativeStream(provider)
    provider.stream_diagnostic = "sse"
    try:
        async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
            async with model_astream(provider, client, "POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    raise LLMProviderError(f"模型请求失败（HTTP {response.status_code}）")
                async for event in EventSource(response).aiter_sse():
                    if event.data:
                        for kind, value in state.feed(json.loads(event.data)):
                            if kind == "delta":
                                yield value["text"]
        state.finish()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise LLMProviderError("模型流式连接或响应异常，请重试") from exc
