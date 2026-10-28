import asyncio
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app import shared_providers
from app.llm import ChatProviderConfig, LLMConfigurationError, OpenAICompatibleChatProvider, _parse_usage
from app.thinking import ThinkingConfig, public_capability, select_thinking
from test_shared_providers import provider, setup_site


def effort(adapter="deepseek", value="high"):
    return {"kind": "effort", "adapter": adapter,
            "options": [{"id": "strong", "label": "高", "value": value}],
            "default_option_id": "strong"}


@pytest.mark.parametrize("raw,params", [
    ({}, {}),
    ({"kind": "unsupported"}, {}),
    ({"kind": "fixed", "adapter": "bailian"}, {}),
    ({"kind": "fixed", "adapter": "deepseek"}, {"thinking": {"type": "enabled"}}),
    (effort(), {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    (effort(value="disabled"), {"thinking": {"type": "disabled"}}),
    (effort("openai", "xhigh"), {"reasoning_effort": "xhigh"}),
    (effort("bailian", "low"), {"enable_thinking": True, "reasoning_effort": "low"}),
    ({"kind": "toggle", "adapter": "deepseek", "options": [
        {"id": "on", "label": "开", "value": "enabled"},
        {"id": "off", "label": "关", "value": "disabled"}], "default_option_id": "off"},
     {"thinking": {"type": "disabled"}}),
    ({"kind": "budget", "adapter": "bailian", "budget_min": 1, "budget_max": 10000,
      "options": [{"id": "small", "label": "精简", "value": 2048}], "default_option_id": "small"},
     {"enable_thinking": True, "thinking_budget": 2048}),
])
def test_capabilities_and_parameter_mapping(raw, params):
    chosen = select_thinking(raw, 7)
    assert chosen.parameters() == params
    public = public_capability(raw, 7)
    assert public["version"] == 7
    assert all("value" not in option for option in public["options"])
    assert "adapter" not in public
    assert chosen.snapshot() is None if not params and raw.get("kind") != "fixed" else chosen.version == 7


@pytest.mark.parametrize("raw", [
    effort("deepseek", "medium"), effort("openai", "invented"),
    {**effort(), "default_option_id": "missing"},
    {**effort(), "options": [{"id": "a", "label": "A", "value": "high"}, {"id": "b", "label": "B", "value": "high"}]},
    {**effort(), "kind": "unsupported"},
    {"kind": "toggle", "adapter": "bailian", "options": [{"id": "on", "label": "开", "value": "enabled"}], "default_option_id": "on"},
    {"kind": "fixed", "adapter": "none"},
    {"kind": "budget", "adapter": "openai"},
    *[{"kind": "budget", "adapter": "bailian", "budget_min": 1, "budget_max": 100,
       "options": [{"id": "small", "label": "精简", "value": value}], "default_option_id": "small"}
      for value in (True, 1.2, "20", -1, 0, 101)],
])
def test_invalid_config_rejected(raw):
    with pytest.raises(ValidationError):
        ThinkingConfig.model_validate(raw)


def test_options_and_version_validation():
    from fastapi import HTTPException
    for raw, option, version, status in [
        (effort(), "missing", 1, 422), (effort(), "strong", 0, 409),
        ({}, "strong", 1, 422), ({"kind": "fixed", "adapter": "bailian"}, "strong", 1, 422),
    ]:
        with pytest.raises(HTTPException) as error:
            select_thinking(raw, 1, option, version)
        assert error.value.status_code == status


def test_all_invocation_modes_use_same_payload_and_hide_reasoning(monkeypatch):
    config = ChatProviderConfig("https://model.example/v1", "secret", "model", thinking=select_thinking(effort(), 1))
    seen = []
    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "answer", "reasoning_content": "private chain"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 15}}})
    monkeypatch.setattr(httpx, "post", post)
    instance = OpenAICompatibleChatProvider(config=config)
    assert instance.complete([]) == "answer"
    response = instance.complete_with_tools([], [{"type": "function", "function": {"name": "x"}}])
    assert response.protocol_fields == {"reasoning_content": "private chain"}
    assert response.content == "answer"
    assert response.usage.completion_tokens == 20
    original = httpx.AsyncClient
    def handler(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    async def collect():
        return [part async for part in instance.stream([])]
    assert asyncio.run(collect()) == ["answer"]
    assert all(item["thinking"] == {"type": "enabled"} and item["reasoning_effort"] == "high" for item in seen)
    assert all("temperature" not in item for item in seen)
    incompatible = replace(config, thinking=select_thinking({**effort("openai"), "supports_tools": False}, 1))
    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleChatProvider(config=incompatible).complete_with_tools([], [{"function": {}}])


def test_admin_config_public_options_independent_versions(accounts, monkeypatch):
    client, db, _ = setup_site(accounts, monkeypatch)
    original = provider(client)
    payload = {key: original[key] for key in ("key", "name", "model", "base_url", "enabled", "version")}
    changed = client.put(f"/site/admin/llm-providers/{original['id']}", json={**payload, "thinking": effort()})
    assert changed.status_code == 200, changed.text
    updated = changed.json()
    assert updated["thinking_version"] == 1
    options = client.get("/models/available").json()["providers"][0]["thinking"]
    assert options == public_capability(effort(), 1)
    from test_shared_providers import prices
    repriced = client.put(f"/site/admin/llm-providers/{original['id']}/pricing",
        json={"version": updated["version"], "policies": prices(8)}).json()
    assert repriced["thinking_version"] == 1
    invalid = client.put(f"/site/admin/llm-providers/{original['id']}",
        json={**payload, "version": repriced["version"], "thinking": effort("deepseek", "medium")})
    assert invalid.status_code == 422
    db.expire_all()
    assert db.get(shared_providers.SiteModelProvider, UUID(original["id"])).thinking_version == 1


def test_stream_only_bailian_collects_tools_and_final_usage(monkeypatch):
    from contextlib import contextmanager
    config = {"kind": "fixed", "adapter": "bailian", "stream_only": True}
    chosen = ChatProviderConfig("https://model.example/v1", "secret", "model", thinking=select_thinking(config, 1))
    @contextmanager
    def streaming(method, url, **kwargs):
        assert kwargs["json"]["stream"]
        assert "temperature" not in kwargs["json"] and "enable_thinking" not in kwargs["json"]
        yield httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"reasoning_content":"secret","tool_calls":[{"index":0,"id":"c1","function":{"name":"lookup","arguments":"{\\\"x\\\":"}}]}}]}\n\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10}}\n\n'
            'data: [DONE]\n\n'))
    monkeypatch.setattr(httpx, "stream", streaming)
    result = OpenAICompatibleChatProvider(config=chosen).complete_with_tools([], [{"function": {}}])
    assert result.tool_calls[0].arguments == {"x": 1}
    assert result.usage.completion_tokens == 20
    assert not result.protocol_fields


def test_agent_continuation_is_encrypted_private_and_model_scoped(accounts, monkeypatch):
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from sqlalchemy import select
    from app import mcp_client, conversations
    from app.models import ChatMessage, SiteModelProvider
    from test_request_models import send
    client, db, identity = setup_site(accounts, monkeypatch)
    first, second = provider(client), provider(client, "second")
    row = db.get(SiteModelProvider, UUID(first["id"]))
    row.thinking_config, row.thinking_version = effort(), 1
    db.commit()
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_credential_key=SecretStr(key)))
    monkeypatch.setattr(shared_providers, "assert_available", lambda _: None)
    observed = []
    def post(url, **kwargs):
        payload = kwargs["json"]
        observed.append(payload)
        if len(observed) == 1:
            row = db.get(SiteModelProvider, UUID(first["id"]))
            row.thinking_config = effort(value="low")
            row.thinking_version = 2
            db.commit()
            message = {"content": None, "reasoning_content": "private-first",
                "tool_calls": [{"id": "call-1", "function": {"name": "missing", "arguments": "{}"}}]}
        else:
            message = {"content": "public answer", "reasoning_content": "private-final"}
        return httpx.Response(200, json={"choices": [{"message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    monkeypatch.setattr(httpx, "post", post)
    conversation = client.post("/conversations", json={}).json()["id"]
    events, _ = send(client, conversation, first["id"], thinking_option_id="strong", thinking_config_version=1)
    final = events[-1].json()
    assert final["status"] == "completed", final
    assert final["thinking"]["config_version"] == 1
    assert all("private" not in event.data for event in events)
    assert all(item["reasoning_effort"] == "high" for item in observed)
    assert observed[1]["messages"][-2]["reasoning_content"] == "private-first"
    saved = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert saved.encrypted_continuation and "private" not in saved.encrypted_continuation
    assert "private" not in client.get(f"/conversations/{conversation}").text
    assert "private" not in client.get(f"/conversations/{conversation}/export").text
    send(client, conversation, first["id"], thinking_config_version=2)
    assert any(item.get("reasoning_content") == "private-first" for item in observed[-1]["messages"])
    send(client, conversation, second["id"])
    assert all("reasoning_content" not in item for item in observed[-1]["messages"])
    assert "reasoning_effort" not in observed[-1]
    assert client.delete(f"/conversations/{conversation}").status_code == 204
    assert client.get(f"/conversations/{conversation}").status_code == 404


def test_invalid_request_choices_do_not_call_or_charge(accounts, monkeypatch):
    from uuid import uuid4
    from sqlalchemy import func, select
    from app.models import SiteModelProvider, CreditLedgerEntry, ChatMessage
    client, db, _ = setup_site(accounts, monkeypatch)
    chosen = provider(client)
    row = db.get(SiteModelProvider, UUID(chosen["id"]))
    row.thinking_config, row.thinking_version = effort(), 4
    db.commit()
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: pytest.fail("Invalid choice reached upstream"))
    conversation = client.post("/conversations", json={}).json()["id"]
    for option, version, status in [("strong", 3, 409), ("missing", 4, 422)]:
        response = client.post(f"/conversations/{conversation}/messages", json={
            "request_id": str(uuid4()), "provider_id": chosen["id"], "query": "question",
            "thinking_option_id": option, "thinking_config_version": version})
        assert response.status_code == status, response.text
    assert not db.scalar(select(func.count()).select_from(ChatMessage))
    assert not db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(CreditLedgerEntry.amount < 0))


def test_single_answer_and_retry_use_current_thinking(accounts, monkeypatch):
    from app import main, conversations
    from app.models import SiteModelProvider, KnowledgeBase
    from app.llm import ToolResponse, LLMUsage, LLMProviderError
    from test_retrieval import add_document
    from test_request_models import send
    client, db, _ = setup_site(accounts, monkeypatch)
    chosen = provider(client)
    raw = effort("openai")
    raw["options"].append({"id": "quick", "label": "低", "value": "low"})
    row = db.get(SiteModelProvider, UUID(chosen["id"]))
    row.thinking_config, row.thinking_version = raw, 1
    db.commit()
    seen = []
    class Model(OpenAICompatibleChatProvider):
        def complete(self, messages, temperature=0):
            seen.append(self.config.thinking.option_id)
            self.last_usage = _parse_usage({"prompt_tokens": 1, "completion_tokens": 1})
            return "answer"
        def complete_with_tools(self, messages, tools, tool_choice):
            seen.append(self.config.thinking.option_id)
            if self.config.thinking.option_id == "strong":
                raise LLMProviderError("controlled failure")
            self.last_usage = LLMUsage(prompt_tokens=1, completion_tokens=1)
            return ToolResponse("answer", [], self.last_usage)
    monkeypatch.setattr(main, "OpenAICompatibleChatProvider", Model)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    library = client.post("/knowledge-bases", json={"name": "Evidence"}).json()
    add_document(db, db.get(KnowledgeBase, UUID(library["id"])), "needle evidence")
    response = client.post(f"/knowledge-bases/{library['id']}/answer", json={
        "query": "needle", "provider_id": chosen["id"], "thinking_option_id": "quick", "thinking_config_version": 1})
    assert response.status_code == 200, response.text
    assert response.json()["thinking"]["label"] == "低"
    conversation = client.post("/conversations", json={}).json()["id"]
    failed, _ = send(client, conversation, chosen["id"])
    retried, _ = send(client, conversation, chosen["id"], query=None, retry_message_id=failed[-1].json()["id"],
                     thinking_option_id="quick", thinking_config_version=1)
    assert retried[-1].json()["thinking"]["option_id"] == "quick"
    assert seen == ["quick", "strong", "quick"]
