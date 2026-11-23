import json
from types import SimpleNamespace
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import select

from app import site_auth, web_search
from app.conversations import execution as chat
from app.model_runtime.client import LLMUsage, ToolCall, ToolResponse
from app.models import ModelBillingSettings, ModelUsageRequest, SearchToolCall, SiteSearchSettings
from test_conversations import client, create, turn


def test_search_fetch_interleaved_persisted_and_single_settlement(client, monkeypatch):
    http, db, _ = client
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(site_auth, "cipher", lambda: cipher)
    db.add(ModelBillingSettings(id=1, enabled=True, shared_pricing_enabled=True))
    db.add(SiteSearchSettings(id=1, enabled=True, encrypted_api_key=site_auth.protect("search-only"),
        prices={key: {"capability": key, "unit": "calls", "unit_size": 1000,
            "credits_per_unit": 5, "enabled": True} for key in web_search.CAPABILITIES}))
    db.commit()
    seen, calls = [], []
    source = {"source_id": "web-test", "type": "web", "url": "https://example.com/news",
        "title": "Public news", "site_name": "example.com", "fetched_at": "2026-10-30T00:00:00Z"}
    def invoke(config, capability, args):
        calls.append(capability)
        return {"effective": True, "result_count": 1, **({"results": [{**source, "summary": "public"}]}
            if capability == "web_search" else {**source, "content": "public page"})}
    monkeypatch.setattr(web_search, "invoke", invoke)

    class Provider:
        last_usage = None
        status = SimpleNamespace(provider_id=None, provider_key=None, model="test")
        def complete_with_tools(self, *args, **kwargs):
            raise AssertionError("must stream")
        def stream_with_tools(self, messages, tools, **_):
            seen.append([tool["function"]["name"] for tool in tools])
            index = len(seen)
            text = ["先搜索。", "再阅读。", "已核实 [web-test]。"][index - 1]
            yield "delta", {"text": text}
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=5)
            call = ToolCall("same-id", "web_search", {"query": "public news"}) if index == 1 else ToolCall(
                "same-id", "web_fetch", {"url": source["url"]})
            yield "complete", ToolResponse(text, [call] if index < 3 else [], usage=self.last_usage)
    def provider(config, cls=Provider):
        result = cls()
        result.status = SimpleNamespace(provider_id=config.provider_id, provider_key=config.provider_key, model=config.model)
        return result
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", provider)
    conversation = create(http, [])
    events, payload = turn(http, conversation["id"])
    done = events[-1][1]
    assert done["status"] == "completed", done.get("error_detail")
    assert calls == ["web_search", "web_fetch"]
    assert all("web_search" in tools and "web_fetch" in tools for tools in seen)
    assert [block["type"] for block in done["display_blocks"]] == ["text", "activity", "text", "activity", "text"]
    expected_source = {**source, "call_ids": [block["call_id"] for block in done["display_blocks"] if block["type"] == "activity"]}
    assert done["web_sources"] == [expected_source] and done["citations"] == []
    assert "content" not in done["web_sources"][0]
    assert http.get(f"/conversations/{conversation['id']}").json()["messages"][-1]["web_sources"] == [expected_source]
    row = db.get(ModelUsageRequest, UUID(payload["request_id"]))
    assert float(row.credits) == .055
    assert len(db.scalars(select(SearchToolCall).where(SearchToolCall.request_id == row.request_id)).all()) == 2

    seen.clear()
    class NoSearch:
        last_usage = None
        status = SimpleNamespace(provider_id=None, provider_key=None, model="test")
        def complete_with_tools(self, *args, **kwargs):
            raise AssertionError("must stream")
        def stream_with_tools(self, messages, tools, **_):
            assert not any(tool["function"]["name"].startswith("web_") for tool in tools)
            yield "complete", ToolResponse("直接回答", [], usage=LLMUsage(prompt_tokens=1, completion_tokens=1))
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda config: provider(config, NoSearch))
    done = turn(http, conversation["id"], web_search=False)[0][-1][1]
    assert done["status"] == "completed" and len(calls) == 2
