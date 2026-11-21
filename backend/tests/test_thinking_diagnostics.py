from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app import shared_providers
from app.llm import _parse_usage
from app.models import BillingSnapshot, ModelBillingSettings, SiteModelProvider, ThinkingTest
from test_shared_providers import provider, setup_site
from test_thinking import effort


@pytest.mark.parametrize("value,expected", [(0, 0), (12, 12), (None, None),
    (True, None), (-1, None), (1.5, None), ("12", None), (21, None)])
def test_reasoning_tokens_are_diagnostic_not_additional_output(value, expected):
    usage = _parse_usage({"prompt_tokens": 10, "completion_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": value}})
    assert usage.reasoning_tokens == expected
    assert usage.measured()["completion_tokens"] == 20


def configured(accounts, monkeypatch):
    client, db, session = setup_site(accounts, monkeypatch)
    item = provider(client)
    row = db.get(SiteModelProvider, UUID(item["id"]))
    row.thinking_config, row.thinking_version = effort("bailian"), 1
    db.commit()
    monkeypatch.setattr(shared_providers, "assert_available", lambda *args: None)
    return client, db, session, item


def test_preview_is_safe_validated_and_never_calls_upstream(accounts, monkeypatch):
    client, db, _, item = configured(accounts, monkeypatch)
    monkeypatch.setattr(httpx, "stream", lambda *_a, **_k: pytest.fail("Preview invoked upstream"))
    url = f"/site/admin/llm-providers/{item['id']}/thinking/preview"
    body = {"version": item["version"], "thinking": effort("bailian"),
        "option_id": "strong", "max_output_tokens": 8192}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["parameters"] == {"enable_thinking": True, "reasoning_effort": "high",
        "max_tokens": 8192, "stream": True, "stream_options": {"include_usage": True}}
    assert "secret" not in response.text and "messages" not in response.text
    assert not db.scalars(select(ThinkingTest)).all()
    assert client.post(url, json={**body, "version": 0}).status_code == 422
    assert client.post(url, json={**body, "version": item["version"] + 1}).status_code == 409
    assert client.post(url, json={**body, "option_id": "unknown"}).status_code == 422
    assert client.post(url, json={**body, "thinking": {**effort("bailian"), "arbitrary": True}}).status_code == 422
    unconfigured = client.post(url, json={**body, "thinking": {}, "option_id": None})
    assert unconfigured.status_code == 200
    assert "temperature" not in unconfigured.json()["parameters"]


@pytest.mark.parametrize("metered", [False, True])
def test_probe_is_metered_idempotent_rate_limited_and_private(accounts, monkeypatch, metered):
    client, db, _, item = configured(accounts, monkeypatch)
    db.get(ModelBillingSettings, 1).enabled = metered
    db.commit()
    seen = []
    @contextmanager
    def streaming(method, url, **kwargs):
        seen.append(kwargs["json"])
        yield httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"reasoning_content":"not public"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"391"},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"completion_tokens_details":{"reasoning_tokens":15}}}\n\n'
            'data: [DONE]\n\n'))
    monkeypatch.setattr(httpx, "stream", streaming)
    url = f"/site/admin/llm-providers/{item['id']}/thinking/test"
    payload = {"request_id": str(uuid4()), "version": item["version"],
        "connection_version": item["connection_version"],
        "thinking_config_version": 1, "option_id": "strong"}
    result = client.post(url, json=payload)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["completed"] and data["received_thinking"] and data["reasoning_tokens"] == 15
    assert "not public" not in result.text and "secret" not in result.text
    assert seen[0]["max_tokens"] == 1024
    assert seen[0]["enable_thinking"] is True
    assert client.post(url, json=payload).json() == data
    assert len(seen) == 1
    assert client.post(url, json={**payload, "option_id": None}).status_code == 409
    for _ in range(2):
        assert client.post(url, json={**payload, "request_id": str(uuid4())}).status_code == 200
    assert client.post(url, json={**payload, "request_id": str(uuid4())}).status_code == 429
    assert len(seen) == 3
    if not metered:
        snapshots = db.scalars(select(BillingSnapshot).where(BillingSnapshot.event_key == "model:0")).all()
        assert all(s.context["call_usage"]["thinking_diagnostic"]["received_thinking"] for s in snapshots)


def test_probe_conflicts_and_permissions_never_call_upstream(accounts, monkeypatch):
    client, db, session, item = configured(accounts, monkeypatch)
    monkeypatch.setattr(httpx, "stream", lambda *_a, **_k: pytest.fail("Invalid probe reached upstream"))
    body = {"request_id": str(uuid4()), "version": item["version"],
        "connection_version": item["connection_version"], "thinking_config_version": 0}
    url = f"/site/admin/llm-providers/{item['id']}/thinking/test"
    assert client.post(url, json=body).status_code == 409
    assert not db.scalars(select(ThinkingTest)).all()
    from app.models import User
    db.get(User, UUID(session["user"]["id"])).is_site_admin = False
    db.commit()
    assert client.post(url, json={**body, "thinking_config_version": 1}).status_code == 403


def test_real_http_thinking_precedes_text_and_final_usage():
    from app.llm import ChatProviderConfig, OpenAICompatibleChatProvider
    from app.thinking import select_thinking
    from test_conversation_streaming import upstream, sse
    from threading import Event
    release, ended = Event(), Event()
    def respond(handler, payload):
        assert payload["enable_thinking"] is True
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        sse(handler, {"choices": [{"delta": {"reasoning_content": "visible upstream thinking"}}]})
        assert release.wait(5), "Thinking was buffered until completion"
        sse(handler, {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
        sse(handler, {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 15}}})
        ended.set()
    with upstream(respond) as url:
        instance = OpenAICompatibleChatProvider(config=ChatProviderConfig(url, "test", "test",
            thinking=select_thinking(effort("bailian"), 1)))
        events = instance.stream_with_tools([], [])
        assert next(events) == ("thinking_delta", {"text": "visible upstream thinking"})
        assert not ended.is_set()
        release.set()
        rest = list(events)
    assert [name for name, _ in rest] == ["thinking_status", "delta", "thinking_status", "complete"]
    assert instance.thinking_diagnostic() == {"received_thinking": True, "reasoning_tokens": 15, "stream": "sse",
        "upstream": {"accepted_response": True, "attempts": 1}}


def test_display_checkpoint_permissions_and_no_private_continuation_fallback(accounts, monkeypatch):
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from app import mcp_client
    from app.models import ChatMessage, Conversation
    from app.thinking import encrypt_display_thinking
    from test_auth import register
    client, db, _, _ = configured(accounts, monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_credential_key=SecretStr(key)))
    conversation = client.post("/conversations", json={}).json()["id"]
    row = ChatMessage(conversation_id=UUID(conversation), sequence=0, role="assistant",
        content="answer", encrypted_display_thinking=encrypt_display_thinking([
            {"id": "thinking-1", "round": 1, "text": "visible-only-through-protected-read"}]),
        encrypted_continuation="invalid-private-ciphertext",
        display_blocks=[{"id": "thinking-1", "type": "thinking", "round": 1, "status": "interrupted"}])
    db.add(row); db.commit()
    url = f"/conversations/{conversation}/messages/{row.id}/thinking"
    assert "visible-only" not in row.encrypted_display_thinking
    assert client.get(url).json()["blocks"][0]["text"] == "visible-only-through-protected-read"
    assert "visible-only" not in client.get(f"/conversations/{conversation}").text
    row.encrypted_display_thinking = None
    db.commit()
    assert client.get(url).json() == {"blocks": []}
    other = accounts[0][1]
    register(other, "thinking-other@example.com")
    assert other.get(url).status_code in (403, 404)
    assert client.delete(f"/conversations/{conversation}").status_code == 204
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("metered", [False, True])
def test_failed_stream_keeps_encrypted_thinking_and_confirmed_usage(accounts, monkeypatch, metered):
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    from app import mcp_client
    from app.models import ChatMessage, ModelCallUsage, CreditLedgerEntry
    from test_request_models import send
    client, db, _, item = configured(accounts, monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_credential_key=SecretStr(key)))
    db.get(ModelBillingSettings, 1).enabled = metered
    db.commit()

    def streaming(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"reasoning_content":"partially returned thinking"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"completion_tokens_details":{"reasoning_tokens":15}}}\n\n'))

    async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        async_client(transport=httpx.MockTransport(streaming), **kwargs))
    conversation = client.post("/conversations", json={}).json()["id"]
    events, _ = send(client, conversation, item["id"])
    final = events[-1].json()
    assert final["status"] == "failed" and final["content"] == ""
    assert final["display_blocks"][0]["status"] == "interrupted"
    row = db.get(ChatMessage, UUID(final["id"]), populate_existing=True)
    assert row.encrypted_display_thinking and "partially returned" not in row.encrypted_display_thinking
    assert client.get(f"/conversations/{conversation}/messages/{row.id}/thinking").json()["blocks"][0]["text"] == "partially returned thinking"
    charges = db.scalars(select(CreditLedgerEntry).where(
        CreditLedgerEntry.kind == "usage_settlement")).all()
    assert len(charges) == 1
    if metered:
        call = db.scalar(select(ModelCallUsage))
        assert call.usage["reasoning_tokens"] == 15 and call.usage["completion_tokens"] == 20
    assert "partially returned" not in client.get(f"/conversations/{conversation}/export").text
