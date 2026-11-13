"""Inspect final HTTP bodies and crash boundaries, with local fake suppliers only."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.agent import AgentCancelled
from app.context_windows import ContextWindowError, make_item
from app.conversation_context import ContextManager
from app.llm import OpenAICompatibleChatProvider, ToolResponse
from app.models import ChatMessage, ContextCheckpoint, ContextOperation, Conversation
from test_context_windows import client, setup_store
from test_conversation_streaming import upstream


def manager_for(client, *, protocol="chat_completions", provider=None):
    db, store, request = setup_store(client)
    config = replace(store.config, api_protocol=protocol, context_capacity=32000, max_output_tokens=1000)
    store.config = config
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = ContextManager(factory, store.access, store.conversation_id, config, provider, None, request, manual=True)
    manager.current_message_id = uuid4()
    manager.current_sequence = 4
    rows = [
        {"role": "user", "content": "USER-SELECTED"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old-call", "type": "function",
            "function": {"name": "lookup", "arguments": '{"value":"PARAM-REMOVED"}'}}]},
        {"role": "tool", "tool_call_id": "old-call", "name": "lookup", "content": "RESULT-REMOVED"},
        {"role": "assistant", "content": "ANSWER-REMOVED " + "details " * 1800},
    ]
    rows[1].update({"_responses_output": [{"type": "reasoning", "encrypted_content": "OPAQUE-REMOVED"},
        {"type": "function_call", "call_id": "old-call", "name": "lookup", "arguments": '{"value":"PARAM-REMOVED"}'}]}
        if protocol == "responses" else {"_native_content": [
            {"type": "thinking", "thinking": "OPAQUE-REMOVED", "signature": "opaque"},
            {"type": "tool_use", "id": "old-call", "name": "lookup", "input": {"value": "PARAM-REMOVED"}}]}
        if protocol == "anthropic" else {"_native_content": [
            {"text": "OPAQUE-REMOVED", "thought": True, "thoughtSignature": "opaque"},
            {"functionCall": {"name": "lookup", "args": {"value": "PARAM-REMOVED"}}}]}
        if protocol == "gemini" else {"reasoning_content": "OPAQUE-REMOVED"})
    state, _ = store.load(db)
    state, _ = store.append(db, [make_item(uuid4(), m, "user" if m["role"] == "user" else
        "tool" if m["role"] == "tool" else "model", request, config=config) for m in rows],
        request_id=request, revision=state.revision)
    db.add(ChatMessage(conversation_id=store.conversation_id, sequence=0, role="assistant", content="DISPLAY-ONLY-REMOVED"))
    db.commit()
    manager.history(db, db.get(Conversation, store.conversation_id)); db.commit()
    manager.start({"role": "system", "content": "CURRENT-SYSTEM"}, "CURRENT-INPUT")
    return db, manager


def json_response(handler, protocol, text="handoff"):
    handler.send_response(200)
    if protocol in ("anthropic", "gemini"):
        from test_conversation_streaming import sse
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if protocol == "anthropic":
            sse(handler, {"type": "message_start", "message": {"usage": {"input_tokens": 10}, "content": []}})
            sse(handler, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            sse(handler, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
            sse(handler, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}})
            sse(handler, {"type": "message_stop"})
        else:
            sse(handler, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})
        return
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    body = ({"choices": [{"message": {"content": text}}]} if protocol == "chat_completions" else
        {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]} if protocol == "responses" else
        {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"} if protocol == "anthropic" else
        {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})
    handler.wfile.write(json.dumps(body).encode())


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_replacement_never_returns_in_http_after_restart_or_second_compact(client, protocol):
    captured = []
    def respond(handler, body):
        captured.append(body)
        json_response(handler, protocol)
    with upstream(respond) as url:
        db, manager = manager_for(client, protocol=protocol)
        config = replace(manager.config, base_url=url, api_key="local-fake-only", site_provider_id=None)
        manager.config = manager.store.config = config
        manager.provider = OpenAICompatibleChatProvider(config=config)
        events = []
        result = manager.prepare(manager.messages, [], events.append, lambda: None)
        assert events[-1][1]["window_id"] == str(manager.window_id)
        list(manager.provider.stream_with_tools(result, []))
        first_window = manager.window_id
        next_request = uuid4()
        db.get(Conversation, manager.conversation_id).active_request_id = next_request
        db.commit()
        restarted = ContextManager(manager.factory, manager.access, manager.conversation_id,
            config, manager.provider, None, next_request, manual=True)
        with manager.factory() as restored_db:
            restarted.history(restored_db, restored_db.get(Conversation, manager.conversation_id)); restored_db.commit()
        assert restarted.window_id == first_window
        restarted.current_message_id = uuid4()
        restarted.start({"role": "system", "content": "UPDATED-SYSTEM"}, "FOLLOWUP")
        restarted.record([{"role": "assistant", "content": "SECOND-REMOVED " + "later " * 1400}], "model", "later")
        second_summary_index = len(captured)
        restarted.prepare(restarted.messages, [], lambda _: None, lambda: None)
        list(restarted.provider.stream_with_tools(restarted.messages, []))
        assert restarted.window_id != first_window
    assert "PARAM-REMOVED" in json.dumps(captured[0])
    for body in [captured[1], *captured[second_summary_index:]]:
        text = json.dumps(body)
        for marker in ("PARAM-REMOVED", "RESULT-REMOVED", "ANSWER-REMOVED", "OPAQUE-REMOVED", "DISPLAY-ONLY-REMOVED"):
            assert marker not in text
        assert text.count("CURRENT-INPUT") == 1
        assert "USER-SELECTED" in text
    assert "SECOND-REMOVED" not in json.dumps(captured[-1])
    assert "UPDATED-SYSTEM" in json.dumps(captured[-1])


class Summary:
    def stream_with_tools(self, *_args, **_kwargs):
        yield "complete", ToolResponse("handoff", [])


def test_cancelled_summary_keeps_old_window(client):
    db, manager = manager_for(client, provider=Summary())
    original = manager.window_id
    checks = []
    def active():
        checks.append(1)
        if len(checks) > 1:
            raise AgentCancelled()
    with pytest.raises(AgentCancelled):
        manager.prepare(manager.messages, [], lambda _: None, active)
    assert manager.store.load(db)[0].window_id == original


def test_completion_event_loss_keeps_installed_window_and_manual_result(client):
    db, manager = manager_for(client, provider=Summary())
    db.add(ContextOperation(request_id=manager.request_id, conversation_id=manager.conversation_id)); db.commit()
    original = manager.window_id
    def emit(event):
        if event[0] == "compaction" and event[1]["status"] == "completed":
            assert "RESULT-REMOVED" not in str(manager.messages)
            raise RuntimeError("lost event / process exit")
    with pytest.raises(RuntimeError):
        manager.prepare(manager.messages, [], emit, lambda: None)
    db.expire_all()
    state, restored = manager.store.load(db)
    assert state.window_id != original
    assert "RESULT-REMOVED" not in str(restored)
    assert db.get(ContextOperation, manager.request_id).status == "completed"


def test_revision_race_does_not_install_candidate(client):
    db, manager = manager_for(client)
    class RacingSummary:
        def stream_with_tools(self, *_args, **_kwargs):
            with manager.factory() as concurrent:
                state, _ = manager.store.load(concurrent)
                manager.store.append(concurrent, [make_item(uuid4(), {"role": "user", "content": "concurrent"}, "user")],
                    request_id=manager.request_id, revision=state.revision)
                concurrent.commit()
            yield "complete", ToolResponse("handoff", [])
    manager.provider = RacingSummary()
    with pytest.raises(ContextWindowError) as error:
        manager.prepare(manager.messages, [], lambda _: None, lambda: None)
    assert error.value.code == "context_revision_conflict"
    assert manager.store.load(db)[0].checkpoint_id is None
