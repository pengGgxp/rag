"""Persistence invariants independent of summary/model availability."""
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import auth
from app.auth import Access
from app.context_windows import ContextWindowError, WindowStore, decrypt_items, make_item, project
from app.llm_providers import request_provider_config
from app.models import ChatMessage, ContextCheckpoint, Conversation, ConversationContext, LoginSession
from test_conversations import client, create
from auth_helpers import model_id


def setup_store(client):
    http, db, _ = client
    identifier = UUID(create(http)["id"])
    session = db.get(LoginSession, auth.digest(http.cookies.get(auth.COOKIE)))
    access = Access(session.user_id, session.workspace_id, "admin", session.token_hash)
    config = request_provider_config(db, access.workspace_id, UUID(model_id(http)))
    request = uuid4()
    db.get(Conversation, identifier).active_request_id = request
    db.commit()
    return db, WindowStore(access, identifier, config), request


def install(store, db, request, state, items, install_id=None):
    return store.install(db, items, request_id=request, revision=state.revision,
        window_id=state.window_id, install_id=install_id or uuid4(), summary="handoff",
        before=10000, after=100, budget={"hard_input": 1000})


def test_replacement_plus_suffix_survives_new_session_and_ignores_display_edits(client):
    db, store, request = setup_store(client)
    chat = ChatMessage(conversation_id=store.conversation_id, sequence=0, role="user", content="OLD-MARKER")
    db.add(chat); db.commit()
    state, old = store.load(db)
    db.commit()
    handoff = make_item(uuid4(), {"role": "user", "content": "summary"}, "handoff", request)
    state, _ = install(store, db, request, state, [handoff]); db.commit()
    new = make_item(uuid4(), {"role": "user", "content": "new question"}, "user", request)
    state, _ = store.append(db, [new], request_id=request, revision=state.revision); db.commit()
    chat.content = "MUTATED-OLD-MARKER"; db.commit()
    db.expunge_all()
    state, restored = store.load(db)
    assert restored == [handoff, new]
    assert "MARKER" not in str(project(restored, store.config))
    assert state.checkpoint_id is not None and len(decrypt_items(state.encrypted_suffix)) == 1


def test_install_is_idempotent_and_rejects_stale_candidate(client):
    db, store, request = setup_store(client)
    state, _ = store.load(db); db.commit()
    revision, window = state.revision, state.window_id
    item = make_item(uuid4(), {"role": "user", "content": "summary"}, "handoff")
    identifier = uuid4()
    install(store, db, request, state, [item], identifier); db.commit()
    store.install(db, [item], request_id=request, revision=revision, window_id=window,
        install_id=identifier, summary="ignored", before=1000, after=1, budget={}); db.commit()
    assert len(list(db.scalars(select(ContextCheckpoint)))) == 1
    with pytest.raises(ContextWindowError, match="上下文已更新"):
        store.install(db, [item], request_id=request, revision=revision, window_id=window,
            install_id=uuid4(), summary="stale", before=1000, after=1, budget={})


def test_append_rollback_does_not_change_restored_window(client):
    db, store, request = setup_store(client)
    state, _ = store.load(db); db.commit()
    store.append(db, [make_item(uuid4(), {"role": "user", "content": "uncommitted"}, "user")],
        request_id=request, revision=state.revision)
    db.rollback()
    assert store.load(db)[1] == []


def test_corrupt_checkpoint_never_falls_back_to_chat(client):
    db, store, request = setup_store(client)
    state, _ = store.load(db); db.commit()
    state, _ = install(store, db, request, state, []); db.commit()
    db.get(ContextCheckpoint, state.checkpoint_id).encrypted_replacement = "broken"
    db.commit()
    with pytest.raises(ContextWindowError) as error:
        store.load(db)
    assert error.value.code == "context_window_unrecoverable"


def test_tool_pairing_and_protocol_switch_protect_pending_suffix(client):
    _, store, request = setup_store(client)
    call = make_item(uuid4(), {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        "_native_content": [{"signature": "PRIVATE"}]}, "model", request, config=store.config)
    switched = replace(store.config, api_protocol="responses")
    with pytest.raises(ContextWindowError) as error:
        project([call], switched)
    assert error.value.code == "context_suffix_incompatible"
    output = make_item(uuid4(), {"role": "tool", "tool_call_id": "a", "content": "result"}, "tool", request)
    assert "PRIVATE" not in str(project([call, output], switched))
    with pytest.raises(ContextWindowError):
        project([output], store.config)


def test_legacy_unknown_same_turn_checkpoint_blocks_without_erasing_chat(client):
    db, store, request = setup_store(client)
    db.add(ChatMessage(conversation_id=store.conversation_id, sequence=0, role="user", content="original"))
    db.add(ContextCheckpoint(conversation_id=store.conversation_id, request_id=request,
        through_sequence=-1, source_digest="x" * 64, scope_digest="x" * 64,
        summary="old", provider_id=UUID(store.config.provider_id), model=store.config.model,
        estimated_before=100, estimated_after=20)); db.commit()
    with pytest.raises(ContextWindowError):
        store.load(db)
    db.commit()
    assert db.get(ConversationContext, store.conversation_id).blocked_reason
    assert db.scalar(select(ChatMessage)).content == "original"
