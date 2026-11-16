"""Full worker completion with image snapshots; local HTTP suppliers only."""
import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import conversation_jobs, conversations, mcp_client
from app.context_windows import WindowStore
from app.extensions import ExtensionRuntime
from app.llm import ChatProviderConfig, OpenAICompatibleChatProvider
from app.llm_providers import request_provider_config
from app.models import (BillingSnapshot, ChatMessage, CreditLedgerEntry, ModelBillingSettings,
    CreditAccount, ModelCallUsage, ModelConnection, SiteModelProvider, UsageEvent)
from app.thinking import encrypt_continuation
from auth_helpers import model_id
from test_chat_attachments import PNG
from test_conversations import client, create, turn
from test_conversation_streaming import compact, sse, upstream
from test_extensions import access_for
from test_thinking import effort


def reply(handler, protocol, *, tool=False, text="红色"):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    arguments = {"computer_id": "00000000-0000-4000-a000-000000000112", "path": "chart.png"}
    if protocol == "responses":
        output = ([{"type": "reasoning", "id": "rs-image", "summary": [],
            "encrypted_content": "private-image-reasoning"}, {"type": "function_call",
            "call_id": "image-call", "name": "read_image", "arguments": json.dumps(arguments)}]
            if tool else [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}])
        sse(handler, {"type": "response.completed", "response": {"status": "completed",
            "output": output, "usage": {"input_tokens": 100, "output_tokens": 25}}})
    elif protocol == "chat_completions":
        delta = ({"reasoning_content": "private-image-reasoning", "tool_calls": [{"index": 0,
            "id": "image-call", "type": "function", "function": {"name": "read_image",
                "arguments": json.dumps(arguments)}}]} if tool else {"content": text})
        sse(handler, {"choices": [{"delta": delta}]})
        sse(handler, {"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25}})
    elif protocol == "anthropic":
        sse(handler, {"type": "message_start", "message": {"usage": {"input_tokens": 100}}})
        block = ({"type": "tool_use", "id": "image-call", "name": "read_image", "input": {}}
            if tool else {"type": "text", "text": ""})
        sse(handler, {"type": "content_block_start", "index": 0, "content_block": block})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(arguments)} if tool else
            {"type": "text_delta", "text": text})
        sse(handler, {"type": "content_block_delta", "index": 0, "delta": delta})
        sse(handler, {"type": "content_block_stop", "index": 0})
        sse(handler, {"type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
            "usage": {"output_tokens": 25}})
        sse(handler, {"type": "message_stop"})
    else:
        part = ({"functionCall": {"name": "read_image", "args": arguments},
            "thoughtSignature": "private-image-signature"} if tool else {"text": text})
        sse(handler, {"candidates": [{"content": {"role": "model", "parts": [part]},
            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 25}})


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "gemini", "chat_completions"])
@pytest.mark.parametrize("metered", [False, True])
def test_image_completion_restore_compact_and_settlement(client, monkeypatch, protocol, metered):
    http, db, _ = client
    provider_id = UUID(model_id(http))
    selected = db.get(SiteModelProvider, provider_id)
    selected.chat_protocol, selected.supports_images = protocol, True
    selected.context_capacity, selected.max_output_tokens = 64000, 4000
    if protocol == "chat_completions":
        selected.thinking_config = effort()
    db.get(ModelConnection, selected.connection_id).api_protocol = protocol
    db.add(ModelBillingSettings(id=1, enabled=metered, shared_pricing_enabled=False))
    db.commit()
    reads, requests = [], []

    def load_image(self, path, check_active, call_id, computer_id, **execution):
        assert execution["agent_tool_execution_id"]
        check_active()
        reads.append((path, call_id))
        return PNG, {"size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}

    monkeypatch.setattr(ExtensionRuntime, "_image_local_loader", load_image)
    removed = "IMAGE-ANSWER-REMOVED " + "old detail " * 1000

    def respond(handler, payload):
        requests.append(payload)
        reply(handler, protocol, tool=len(requests) == 1,
            text=removed if len(requests) == 2 else "handoff" if len(requests) == 4 else "红色")

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    encoded = base64.b64encode(PNG).decode()
    with upstream(respond) as url:
        monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda config:
            OpenAICompatibleChatProvider(config=replace(config, base_url=url, site_provider_id=None)))
        conversation = create(http)
        conversation_id = UUID(conversation["id"])
        events, first = turn(http, str(conversation_id), "IMAGE-CURRENT-INPUT 查看 chart.png")
        assert events[-1][1]["status"] == "completed", events[-1]
        assert len(reads) == 1 and len(requests) == 2
        assert encoded not in json.dumps(requests[0]) and encoded in json.dumps(requests[1])
        assert json.dumps(requests[1]).count("IMAGE-CURRENT-INPUT") == 1
        answer = db.scalar(select(ChatMessage).where(ChatMessage.request_id == UUID(first["request_id"]),
            ChatMessage.role == "assistant"))
        audit = json.loads(mcp_client.decrypt_headers(answer.encrypted_continuation)["continuation"])
        assert "model_image" not in json.dumps(audit) and encoded not in json.dumps(audit)
        assert "read_image" in json.dumps(audit)
        if protocol in ("responses", "chat_completions"):
            assert "private-image-reasoning" in json.dumps(audit)
        if protocol == "gemini":
            assert "private-image-signature" in json.dumps(audit)
        access = access_for(http, db)
        config = request_provider_config(db, access.workspace_id, provider_id)
        store = WindowStore(access, conversation_id, config)
        with factory() as restored:
            state, items = store.load(restored)
            original_window = state.window_id
            assert PNG in image_bytes(items)
            restored.commit()
        # Refresh/SSE replay and a fresh worker session must not replay tools or billing.
        assert http.get(f"/conversations/{conversation_id}").json()["messages"][-1]["status"] == "completed"
        before = settlement_snapshot(db)
        assert http.get(f"/conversations/{conversation_id}/requests/{first['request_id']}/events").status_code == 200
        conversation_jobs.run(first["request_id"], factory=factory)
        conversation_jobs.recover(factory=factory)
        assert settlement_snapshot(db) == before
        assert len(reads) == 1 and len(requests) == 2
        followup, _ = turn(http, str(conversation_id), "IMAGE-FOLLOWUP 继续看已有图片")
        assert followup[-1][1]["status"] == "completed", followup[-1]
        assert encoded in json.dumps(requests[2])
        assert len(reads) == 1
        compacted = compact(http, str(conversation_id), str(provider_id))
        assert compacted[-1][1]["status"] == "completed", compacted[-1]
        with factory() as restored:
            state, items = store.load(restored)
            assert state.window_id != original_window and not image_bytes(items)
            restored.commit()
        final, _ = turn(http, str(conversation_id), "AFTER-COMPACT 继续")
        assert final[-1][1]["status"] == "completed", final[-1]
        assert len(requests) == 5 and len(reads) == 1
        wire = json.dumps(requests[-1])
        for marker in ("IMAGE-ANSWER-REMOVED", "private-image-reasoning", "private-image-signature", encoded):
            assert marker not in wire
        assert wire.count("AFTER-COMPACT") == 1
        assert wire.count("IMAGE-CURRENT-INPUT") == 1
        assert "IMAGE-ANSWER-REMOVED" in json.dumps(http.get(f"/conversations/{conversation_id}").json())
    if metered:
        calls = db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == UUID(first["request_id"]))).all()
    else:
        calls = db.scalars(select(BillingSnapshot).where(BillingSnapshot.request_id == UUID(first["request_id"]),
            BillingSnapshot.event_key.like("model:%"))).all()
    assert len(calls) == 2
    entries = db.scalars(select(CreditLedgerEntry).where(
        CreditLedgerEntry.metadata_json["request_id"].as_string() == first["request_id"])).all()
    kinds = [row.kind for row in entries]
    assert sorted(kinds) == ["usage_hold", "usage_hold_release", "usage_settlement"]


def image_bytes(value):
    if isinstance(value, dict):
        return [value["data"]] if value.get("type") == "model_image" else [
            data for item in value.values() for data in image_bytes(item)]
    if isinstance(value, list):
        return [data for item in value for data in image_bytes(item)]
    return []


def settlement_snapshot(db):
    db.expire_all()
    snapshot = {model.__name__: [tuple(str(getattr(row, column.key)) for column in model.__mapper__.column_attrs)
        for row in db.scalars(select(model).order_by(model.id))]
        for model in (CreditLedgerEntry, BillingSnapshot, ModelCallUsage, UsageEvent)}
    snapshot["accounts"] = [(row.balance, row.lifetime_spent)
        for row in db.scalars(select(CreditAccount).order_by(CreditAccount.workspace_id))]
    return snapshot


@pytest.mark.parametrize("native", [False, True])
def test_audit_image_scrubbing_does_not_mutate_active_messages(monkeypatch, native):
    image = {"type": "model_image", "data": PNG, "filename": "chart.png",
        "snapshot_id": "stable-image", "sha256": hashlib.sha256(PNG).hexdigest()}
    messages = [{"role": "assistant", "content": None, "reasoning_content": "private",
        "_native_content": [{"signature": "native-signature"}],
        "_responses_output": [{"encrypted_content": "opaque-state"}],
        "tool_calls": [{"id": "image-call", "function": {"name": "read_image", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "image-call", "content": "loaded"}]
    if native:
        messages[-1]["_image_input"] = image
    else:
        messages.append({"role": "user", "content": [image]})
    original = deepcopy(messages)
    monkeypatch.setattr(mcp_client, "encrypt_headers", lambda value: value)
    encrypted = encrypt_continuation(ChatProviderConfig("https://example.test", "test", "vision",
        api_protocol="responses"), messages)
    audit = json.loads(encrypted["continuation"])["messages"]
    assert messages == original and image_bytes(messages) == [PNG]
    assert audit[0] == messages[0] and audit[1]["tool_call_id"] == "image-call"
    assert not image_bytes(audit) and base64.b64encode(PNG).decode() not in encrypted["continuation"]
