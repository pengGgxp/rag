"""Validated model capabilities and immutable request-scoped thinking settings."""
from dataclasses import dataclass
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

EFFORTS = {
    "deepseek": ("disabled", "low", "high", "max"),
    "openai": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "bailian": ("low", "medium", "high"),
}


class ThinkingOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    label: str = Field(min_length=1, max_length=40)
    value: StrictStr | StrictInt


class ThinkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["unconfigured", "unsupported", "fixed", "toggle", "effort", "budget"] = "unconfigured"
    adapter: Literal["none", "deepseek", "bailian", "openai"] = "none"
    options: list[ThinkingOption] = Field(default_factory=list, max_length=10)
    default_option_id: str | None = None
    budget_min: int = Field(default=1, ge=1, le=2_147_483_647, strict=True)
    budget_max: int = Field(default=32768, ge=1, le=2_147_483_647, strict=True)
    supports_tools: bool = True
    stream_only: bool = False
    always_thinking: bool = False

    @model_validator(mode="after")
    def valid_capability(self):
        if self.kind in ("unconfigured", "unsupported"):
            if self.adapter != "none" or self.options or self.default_option_id or self.stream_only or self.always_thinking:
                raise ValueError("未配置或不支持时不能设置适配器和选项")
            return self
        if self.adapter == "none":
            raise ValueError("请选择接口适配器")
        if self.stream_only and self.adapter != "bailian":
            raise ValueError("仅百炼适配器支持仅流式模式")
        if self.always_thinking and (self.adapter != "bailian" or self.kind == "toggle"):
            raise ValueError("固定开启仅适用于百炼的固定思考、等级或预算")
        if self.kind == "fixed":
            if self.options or self.default_option_id:
                raise ValueError("固定思考不接受可选档位")
            return self
        if self.adapter == "openai" and self.kind != "effort":
            raise ValueError("OpenAI 适配器仅支持等级或固定思考")
        if self.kind == "budget" and self.adapter != "bailian":
            raise ValueError("预算预设仅适用于百炼适配器")
        ids = [option.id for option in self.options]
        values = [option.value for option in self.options]
        if not ids or len(ids) != len(set(ids)) or len(values) != len(set(values)):
            raise ValueError("选项标识和实际参数不可重复")
        if self.default_option_id not in ids:
            raise ValueError("默认选项必须来自已配置选项")
        if self.kind == "toggle" and set(values) != {"enabled", "disabled"}:
            raise ValueError("开关必须包含 enabled 和 disabled")
        if self.kind == "effort" and any(value not in EFFORTS[self.adapter] for value in values):
            raise ValueError("包含适配器不支持的思考等级")
        if self.always_thinking and "disabled" in values:
            raise ValueError("始终思考的模型不能配置关闭选项")
        if self.kind == "budget":
            if self.budget_min > self.budget_max or any(
                type(value) is not int or not self.budget_min <= value <= self.budget_max for value in values
            ):
                raise ValueError("预算必须为模型支持范围内的整数")
        return self


@dataclass(frozen=True)
class ThinkingSelection:
    adapter: str = "none"
    kind: str = "unconfigured"
    option_id: str | None = None
    label: str | None = None
    value: str | int | None = None
    version: int = 0
    supports_tools: bool = True
    stream_only: bool = False
    always_thinking: bool = False

    def parameters(self):
        if self.adapter == "none":
            return {}
        if self.adapter == "deepseek":
            result = {"thinking": {"type": "disabled" if self.value == "disabled" else "enabled"}}
            if self.kind == "effort" and self.value != "disabled":
                result["reasoning_effort"] = self.value
            return result
        if self.adapter == "openai":
            return {"reasoning_effort": self.value} if self.kind == "effort" else {}
        result = {} if self.kind == "fixed" or self.always_thinking else {"enable_thinking": self.value != "disabled"}
        if self.kind == "budget":
            result["thinking_budget"] = self.value
        if self.kind == "effort":
            result["reasoning_effort"] = self.value
        return result

    @property
    def continuation_required(self):
        return self.adapter == "deepseek" and self.value != "disabled"

    @property
    def displays_reasoning(self):
        return self.adapter in ("deepseek", "bailian") and self.kind != "unsupported"

    def snapshot(self):
        if self.kind in ("unconfigured", "unsupported"):
            return None
        return {"option_id": self.option_id, "label": self.label or "固定思考",
                "config_version": self.version, "kind": self.kind}


def public_capability(raw, version):
    config = ThinkingConfig.model_validate(raw or {})
    return {"kind": config.kind, "version": version, "default_option_id": config.default_option_id,
            "options": [{"id": item.id, "label": item.label,
                         "detail": f"{item.value} Token" if config.kind == "budget" else str(item.value)}
                        for item in config.options]}


def select_thinking(raw, version, option_id=None, expected_version=None):
    config = ThinkingConfig.model_validate(raw or {})
    if expected_version is not None and expected_version != version:
        raise HTTPException(409, "模型思考配置已更新，请重新加载模型并确认思考设置")
    if config.kind in ("unconfigured", "unsupported", "fixed") and option_id is not None:
        raise HTTPException(422, "当前模型不接受思考选项")
    chosen = option_id if option_id is not None else config.default_option_id
    option = next((item for item in config.options if item.id == chosen), None)
    if chosen is not None and option is None:
        raise HTTPException(422, "当前模型不支持所选思考设置")
    return ThinkingSelection(
        adapter=config.adapter, kind=config.kind, option_id=chosen,
        label=option.label if option else None, value=option.value if option else None,
        version=version, supports_tools=config.supports_tools, stream_only=config.stream_only,
        always_thinking=config.always_thinking)


def encrypt_continuation(config, messages):
    if not config.continuation_required:
        return None
    from .mcp_client import encrypt_headers
    return encrypt_headers({"continuation": json.dumps({
        "provider": config.site_provider_id, "model": config.model,
        "protocol": config.api_protocol, "connection": config.connection_id,
        "adapter": config.thinking.adapter, "messages": messages,
    }, ensure_ascii=True)})


def history_message(row, config, skill_versions=None):
    if row.role == "user" and getattr(row, "attachments", None):
        from .chat_attachments import user_content
        return [{"role": "user", "content": user_content(row.content, row.attachments)}]
    if row.role == "assistant" and row.encrypted_continuation and config.continuation_required:
        from .mcp_client import decrypt_headers, MCPError
        from .llm import LLMConfigurationError
        try:
            private = json.loads(decrypt_headers(row.encrypted_continuation)["continuation"])
        except (MCPError, ValueError, KeyError) as exc:
            raise LLMConfigurationError("历史模型上下文无法恢复，请联系管理员或新建对话") from exc
        if (private.get("provider") == config.site_provider_id and private.get("model") == config.model
                and private.get("adapter") == config.thinking.adapter):
            if (private.get("protocol", "chat_completions") == config.api_protocol
                    and private.get("connection", config.connection_id) == config.connection_id):
                messages = private["messages"]
                allowed_skills = dict(skill_versions) if skill_versions is not None else None
                for message in messages:
                    for call in message.get("tool_calls") or []:
                        name = call.get("function", {}).get("name", "")
                        if name == "read_skill_file":
                            return [{"role": row.role, "content": row.content}]
                        if name.startswith("mcp_"):
                            result = next((item for item in messages if item.get("role") == "tool"
                                and item.get("tool_call_id") == call.get("id")), None)
                            try:
                                binding = json.loads(result["content"]).get("_extension", {}) if result else {}
                            except (ValueError, TypeError, AttributeError):
                                binding = {}
                            if (allowed_skills is None or allowed_skills.get(name) != binding.get("version")
                                    or binding.get("tool") != name):
                                return [{"role": row.role, "content": row.content}]
                        if name in ("load_skill", "read_skill_resource"):
                            result = next((item for item in messages if item.get("role") == "tool"
                                and item.get("tool_call_id") == call.get("id")), None)
                            try:
                                loaded = json.loads(result["content"]) if result else {}
                            except (ValueError, TypeError):
                                loaded = {}
                            if (not isinstance(loaded, dict) or not isinstance(loaded.get("id"), str)
                                    or type(loaded.get("version")) is not int
                                    or allowed_skills is not None and allowed_skills.get(loaded["id"]) != loaded["version"]):
                                return [{"role": row.role, "content": row.content}]
                return messages
    return [{"role": row.role, "content": row.content}]


def encrypt_display_thinking(blocks):
    if not blocks:
        return None
    from .mcp_client import encrypt_headers
    return encrypt_headers({"display_thinking": json.dumps(blocks, ensure_ascii=True)})


def decrypt_display_thinking(value):
    if not value:
        return []
    from .mcp_client import decrypt_headers
    return json.loads(decrypt_headers(value)["display_thinking"])
