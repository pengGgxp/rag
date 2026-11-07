from uuid import UUID, uuid4

import pytest
from httpx_sse import EventSource
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import conversations, main
from app.llm import LLMProviderError, LLMUsage, OpenAICompatibleChatProvider, ToolCall, ToolResponse
from app.models import (BillingSnapshot, ChatMessage, CreditAccount, CreditLedgerEntry,
                        LLMProviderConfig, ModelCallUsage, ModelBillingSettings, SiteModelProvider,
                        UsageEvent, UsagePolicy, Workspace)
from test_auth import register, share, switch
from test_shared_providers import setup_site, provider
from test_retrieval import add_document
from auth_helpers import StreamingTestProvider, generation_response


def send(client, conversation, identifier, **changes):
    body = {"request_id": str(uuid4()), "provider_id": identifier, "query": "hello", **changes}
    result = client.post(f"/conversations/{conversation}/messages", json=body)
    assert result.status_code == 200, result.text
    return list(EventSource(generation_response(result)).iter_sse()), body


@pytest.mark.parametrize("settlement", [False, True])
def test_each_request_selects_actual_model_and_pins_prices(accounts, monkeypatch, settlement):
    client, db, identity = setup_site(accounts, monkeypatch)
    first, second = provider(client, "first", 2), provider(client, "second", 7)
    settings = db.get(ModelBillingSettings, 1)
    settings.enabled = settlement
    db.commit()
    workspace = UUID(identity["workspace_id"])
    observed = []

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            observed.append(self.config)
            with Session(db.get_bind()) as independent:
                row = independent.get(SiteModelProvider, UUID(self.config.site_provider_id))
                row.name = "Renamed"
                row.model = "changed-after-admission"
                row.prices = {key: {**value, "credits_per_unit": 999} for key, value in row.prices.items()}
                independent.commit()
            usage = LLMUsage(prompt_tokens=1000, completion_tokens=1000, cached_input_tokens=0)
            self.last_usage = usage
            return ToolResponse("answer", [], usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    conversation = client.post("/conversations", json={}).json()["id"]
    for chosen, rate in ((first, 2), (second, 7)):
        events, body = send(client, conversation, chosen["id"])
        answer = events[-1].json()
        assert answer["status"] == "completed"
        assert answer["provider_name"] == chosen["name"] and answer["model"] == "model-one"
        entries = db.scalars(select(UsageEvent).where(UsageEvent.request_id == UUID(body["request_id"]))).all()
        assert sum(item.credits for item in entries) == rate * 2
        binding = db.get(LLMProviderConfig, UUID(answer["provider_id"]))
        assert binding.site_provider_id == UUID(chosen["id"]) and not binding.active
        assert all(item.provider_id == binding.id for item in entries)
        assert all(item.policy_snapshot["credits_per_unit"] == rate for item in entries)
        assert client.post(f"/conversations/{conversation}/messages", json={**body, "provider_id": str(uuid4())}).status_code == 200
    assert len(observed) == 2
    assert db.scalar(select(func.count()).select_from(UsagePolicy)) == 0
    assert db.get(Workspace, workspace).model_version == 0
    assert db.get(CreditAccount, workspace).balance == 982
    detail = client.get(f"/conversations/{conversation}").json()
    assert [item["provider_name"] for item in detail["messages"] if item["role"] == "assistant"] == ["first", "second"]
    assert [item["site_provider_id"] for item in detail["messages"] if item["role"] == "assistant"] == [first["id"], second["id"]]
    assert "provider_id" not in detail  # No conversation-level selected model.


def test_member_can_choose_any_configured_model(accounts, monkeypatch):
    owner, db, identity = setup_site(accounts, monkeypatch)
    public, private = provider(owner, "public"), provider(owner, "legacy")
    db.get(SiteModelProvider, UUID(private["id"])).published = False
    db.commit()
    (_, member, anonymous), _ = accounts
    registered = register(member, "member@example.com")
    share(owner, registered["user"]["email"], "viewer")
    switch(member, identity["workspace_id"])
    assert anonymous.get("/models/available").status_code == 401
    data = member.get("/models/available")
    assert public["id"] in data.text and private["id"] in data.text
    assert all(key not in data.text for key in ("secret", "base_url", "prices", "api_key"))
    from app import shared_providers
    binding, _ = shared_providers.request_binding(db, UUID(identity["workspace_id"]), UUID(private["id"]))
    assert binding.site_provider_id == UUID(private["id"])
    db.commit()
    switch(member, registered["workspace_id"])
    assert private["id"] in member.get("/models/available").text


def test_missing_id_invalid_model_and_disabled_never_charge(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    chosen = provider(client)
    conversation = client.post("/conversations", json={}).json()["id"]
    path = f"/conversations/{conversation}/messages"
    assert client.post(path, json={"query": "hello", "request_id": str(uuid4())}).status_code == 422
    for identifier, expected in ((str(uuid4()), 404), (chosen["id"], 409)):
        db.get(SiteModelProvider, UUID(chosen["id"])).enabled = False
        db.commit()
        response = client.post(path, json={"provider_id": identifier, "query": "hello", "request_id": str(uuid4())})
        assert response.status_code == expected
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(CreditLedgerEntry.amount < 0)) == 0


def test_real_retry_uses_current_choice_and_old_messages_remain_unknown(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    first, second = provider(client, "first"), provider(client, "second")
    calls = []

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            calls.append(self.config.site_provider_id)
            if self.config.site_provider_id == first["id"]:
                raise LLMProviderError("controlled failure")
            self.last_usage = LLMUsage(prompt_tokens=1, completion_tokens=1)
            return ToolResponse("retry worked", [], self.last_usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    conversation = client.post("/conversations", json={}).json()["id"]
    events, _ = send(client, conversation, first["id"])
    failed = events[-1].json()
    assert failed["status"] == "failed"
    retried, _ = send(client, conversation, second["id"], query=None, retry_message_id=failed["id"])
    assert retried[-1].json()["provider_name"] == "second"
    assert retried[-1].json()["site_provider_id"] == second["id"]
    assert calls == [first["id"], second["id"]]
    db.add(ChatMessage(conversation_id=UUID(conversation), sequence=20, role="assistant", content="old"))
    db.commit()
    old = client.get(f"/conversations/{conversation}").json()["messages"][-1]
    assert old["provider_id"] is old["provider_name"] is old["model"] is None
    assert old["site_provider_id"] is None


def test_model_history_identity_remains_readable_after_disabling(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    chosen = provider(client)
    from auth_helpers import StreamingTestProvider
    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            self.last_usage = LLMUsage(prompt_tokens=1, completion_tokens=1)
            return ToolResponse("done", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    conversation = client.post("/conversations", json={}).json()["id"]
    send(client, conversation, chosen["id"])
    db.get(SiteModelProvider, UUID(chosen["id"])).enabled = False
    db.commit()
    assert client.get("/models/available").json()["providers"] == []
    detail = client.get(f"/conversations/{conversation}").json()
    assert detail["messages"][-1]["site_provider_id"] == chosen["id"]
    assert all(key not in detail["messages"][-1] for key in ("base_url", "api_key", "prices", "encrypted_api_key"))
    (_, outsider, _), _ = accounts
    register(outsider, "other-model-history@example.com")
    assert outsider.get(f"/conversations/{conversation}").status_code == 404


def test_single_answer_uses_explicit_model(accounts, monkeypatch):
    client, db, identity = setup_site(accounts, monkeypatch)
    chosen = provider(client)
    library = client.post("/knowledge-bases", json={"name": "Evidence"}).json()
    from app.models import KnowledgeBase
    add_document(db, db.get(KnowledgeBase, UUID(library["id"])), "needle is useful evidence")
    seen = []

    class Model(StreamingTestProvider):
        def complete(self, messages, temperature=0):
            seen.append(self.config.site_provider_id)
            self.last_usage = LLMUsage(prompt_tokens=1, completion_tokens=1)
            return "confirmed answer"

    monkeypatch.setattr(main, "OpenAICompatibleChatProvider", Model)
    response = client.post(f"/knowledge-bases/{library['id']}/answer",
        json={"query": "needle", "provider_id": chosen["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "model-one" and seen == [chosen["id"]]


@pytest.mark.parametrize("settlement", [False, True])
@pytest.mark.parametrize("disable", [False, True])
def test_agent_rounds_keep_request_model_and_price_snapshot(accounts, monkeypatch, settlement, disable):
    client, db, identity = setup_site(accounts, monkeypatch)
    chosen = provider(client, rate=2)
    db.get(ModelBillingSettings, 1).enabled = settlement
    db.commit()
    seen = []

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            seen.append((self.config.site_provider_id, self.config.model))
            self.last_usage = LLMUsage(prompt_tokens=1000, completion_tokens=1000)
            if len(seen) == 1:
                with Session(db.get_bind()) as independent:
                    row = independent.get(SiteModelProvider, UUID(chosen["id"]))
                    row.prices = {key: {**value, "credits_per_unit": 999} for key, value in row.prices.items()}
                    row.model = "edited-model"
                    row.enabled = not disable
                    independent.commit()
                return ToolResponse(None, [ToolCall("tool-1", "unavailable_tool", {})], self.last_usage)
            return ToolResponse("done", [], self.last_usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    conversation = client.post("/conversations", json={}).json()["id"]
    events, body = send(client, conversation, chosen["id"])
    answer = events[-1].json()
    assert answer["model"] == "model-one"
    assert len(seen) == (1 if disable else 2)
    assert all(item == (chosen["id"], "model-one") for item in seen)
    entries = db.scalars(select(UsageEvent).where(UsageEvent.request_id == UUID(body["request_id"]))).all()
    if not disable:
        assert answer["status"] == "completed"
        assert sum(row.credits for row in entries) == 8
        assert all(row.policy_snapshot["credits_per_unit"] == 2 for row in entries)
    else:
        assert answer["status"] != "completed"
        assert all(row.policy_snapshot["credits_per_unit"] == 2 for row in entries)
