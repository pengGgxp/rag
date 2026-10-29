"""Read-only, opt-in real-provider checks; run inside the API container."""
import argparse
import json
from dataclasses import asdict
from types import SimpleNamespace

from sqlalchemy import text

from app.db import SessionLocal
from app.llm import ChatProviderConfig, LLMProviderError, OpenAICompatibleChatProvider
from app.llm_providers import _decrypt_api_key
from app.thinking import select_thinking


def check(row, raw, *, tools=False):
    config = ChatProviderConfig(
        base_url=row.base_url, api_key=_decrypt_api_key(row.encrypted_api_key),
        model=row.model, timeout_seconds=min(row.timeout_seconds, 60),
        thinking=select_thinking(raw, 1))
    model = OpenAICompatibleChatProvider(config=config)
    result = {"model": row.model, "kind": raw["kind"], "parameters": config.thinking.parameters()}
    try:
        if tools:
            definitions = [{"type": "function", "function": {"name": "check_number",
                "description": "Return whether a number is even.",
                "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}}]
            messages = [{"role": "user", "content": "Call check_number with value 2, then briefly report its result."}]
            first = model.complete_with_tools(messages, definitions)
            result["tool_calls"] = len(first.tool_calls)
            result["continuation_present"] = bool(first.protocol_fields and first.protocol_fields.get("reasoning_content"))
            result["first_usage"] = asdict(first.usage) if first.usage else None
            if not first.tool_calls:
                result["status"] = "unverified_no_tool_call"
                return result
            messages.append({"role": "assistant", "content": first.content, **(first.protocol_fields or {}),
                "tool_calls": [{"id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.arguments)}} for call in first.tool_calls]})
            for call in first.tool_calls:
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": json.dumps({"is_even": True})})
            answer = model.complete_with_tools(messages, definitions)
            result["answer_present"] = bool(answer.content)
            result["usage"] = asdict(answer.usage) if answer.usage else None
            result["status"] = "passed" if answer.content else "unverified_no_final_answer"
        else:
            answer = model.complete([{"role": "user", "content": "Reply with OK only."}])
            result.update(status="passed" if answer else "failed_empty_answer",
                          usage=asdict(model.last_usage) if model.last_usage else None)
    except Exception as exc:
        # Do not echo upstream bodies, prompts, credentials or reasoning.
        result.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, LLMProviderError):
            result["error"] = str(exc)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--adapter", choices=["deepseek", "bailian"], required=True)
    args = parser.parse_args()
    from uuid import UUID
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        data = db.execute(text("SELECT c.base_url, c.encrypted_api_key, m.model, c.timeout_seconds, "
            "(m.enabled AND c.enabled) AS enabled FROM site_model_providers m "
            "JOIN model_connections c ON c.id=m.connection_id WHERE m.id=:id"), {"id": UUID(args.provider_id)}).mappings().one_or_none()
        row = SimpleNamespace(**data) if data else None
        if row is None or not row.enabled:
            raise SystemExit("Enabled provider required")
        if args.adapter == "deepseek":
            toggle = {"adapter": "deepseek", "kind": "toggle", "options": [
                {"id": "on", "label": "On", "value": "enabled"}, {"id": "off", "label": "Off", "value": "disabled"}],
                "default_option_id": "off"}
            print(json.dumps(check(row, toggle)))
            effort = {"adapter": "deepseek", "kind": "effort", "options": [
                {"id": "low", "label": "Low", "value": "low"}], "default_option_id": "low"}
            print(json.dumps(check(row, effort)))
            print(json.dumps(check(row, effort, tools=True)))
        else:
            budget = {"adapter": "bailian", "kind": "budget", "stream_only": True,
                "options": [{"id": "small", "label": "Small", "value": 1024}], "default_option_id": "small"}
            print(json.dumps(check(row, budget)))
            print(json.dumps(check(row, budget, tools=True)))
