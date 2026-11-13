"""Main's replacement windows must survive the ordinary-resource Skill change."""
from uuid import uuid4

import pytest

from app.conversation_context import ContextManager, digest, scope_digest
from app.context_windows import ContextWindowError, make_item
from app.models import AgentSkill, ContextCheckpoint, Conversation, ConversationContext, MCPServer, SiteSkill
from test_context_windows import client, install, setup_store


@pytest.fixture
def scope_case(client):
    db, store, request = setup_store(client)
    conversation = db.get(Conversation, store.conversation_id)
    server = MCPServer(workspace_id=store.access.workspace_id, owner_id=store.access.user_id,
        name="Local fixture", url="https://example.test/mcp", enabled=True,
        version=1, authorization_version=1)
    db.add(server); db.commit()
    return db, store, request, conversation, server


def checkpoint(scope_case):
    db, store, request, conversation, server = scope_case
    state, _ = store.load(db); db.commit()
    handoff = make_item(uuid4(), {"role": "user", "content": "Private handoff"}, "handoff", request)
    state, _ = install(store, db, request, state, [handoff]); db.commit()
    return state.window_id, handoff


def test_public_manager_is_main_replacement_manager():
    from app.effective_context import ContextManager as EffectiveContextManager
    assert ContextManager is EffectiveContextManager


def test_unchanged_scope_keeps_existing_main_digest_shape(scope_case):
    db, store, _, conversation, server = scope_case
    expected = digest({"libraries": [], "documents": [], "extensions": [], "personal": [],
        "remote": [(str(server.id), 1, 1, True)],
        "selection": [[], [], False], "role": "admin", "user": str(store.access.user_id)})
    assert scope_digest(db, conversation, store.access) == expected


@pytest.mark.parametrize("field,value", [("authorization_version", 2), ("version", 2), ("enabled", False)])
def test_mcp_changes_block_private_handoff_without_loading_display_history(scope_case, field, value):
    db, store, _, _, server = scope_case
    window, _ = checkpoint(scope_case)
    setattr(server, field, value); db.commit()
    with pytest.raises(ContextWindowError) as error:
        store.load(db)
    assert error.value.code == "context_window_unrecoverable"
    db.rollback()
    state = db.get(ConversationContext, store.conversation_id)
    assert db.get(ContextCheckpoint, state.checkpoint_id).window_id == window


@pytest.mark.parametrize("field,value", [("mcp_server_ids", ["other-selection"]), ("question_workbench", True)])
def test_current_tool_selection_still_invalidates_private_handoff(scope_case, field, value):
    db, store, _, conversation, _ = scope_case
    checkpoint(scope_case)
    setattr(conversation, field, value); db.commit()
    with pytest.raises(ContextWindowError):
        store.load(db)


@pytest.mark.parametrize("kind", ["platform", "personal", "selection"])
def test_retired_skill_records_do_not_invalidate_resource_based_context(scope_case, kind):
    db, store, _, conversation, _ = scope_case
    window, handoff = checkpoint(scope_case)
    if kind == "platform":
        db.add(SiteSkill(name="Historical", description="Unused", markdown="Old body", enabled=True,
            version=7, created_by=store.access.user_id, updated_by=store.access.user_id))
    elif kind == "personal":
        db.add(AgentSkill(workspace_id=store.access.workspace_id, owner_id=store.access.user_id,
            name="Historical", description="Unused", markdown="Old body", enabled=True, version=7))
    else:
        conversation.skill_ids = ["retired-selection"]
    db.commit()
    state, items = store.load(db)
    assert state.window_id == window and items == [handoff]


def test_legacy_scope_keeps_old_platform_skill_hash_for_checkpoint_validation(scope_case):
    db, store, _, conversation, _ = scope_case
    old = SiteSkill(name="Historical", description="Unused", markdown="Old body", enabled=True,
        version=7, created_by=store.access.user_id, updated_by=store.access.user_id)
    db.add(old); db.commit()
    expected = digest({"libraries": [], "documents": [], "extensions": [(str(old.id), 7)],
        "role": "admin", "user": str(store.access.user_id)})
    assert scope_digest(db, conversation, store.access, legacy=True) == expected
    explicit = digest({"libraries": [], "documents": [], "extensions": [],
        "role": "admin", "user": str(store.access.user_id)})
    assert scope_digest(db, conversation, store.access, [], legacy=True) == explicit
