"""Main's replacement windows must survive the ordinary-resource Skill change."""
from uuid import uuid4

import pytest

from app.conversation_context import ContextManager, digest, scope_digest
from app.context_windows import ContextWindowError, make_item
from app.models import AgentSkill, ContextCheckpoint, Conversation, ConversationContext, MCPServer, SiteSkill
from test_context_windows import client, install, setup_store
from test_resources import resource_store


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


def test_each_message_and_compact_recovery_uses_current_skill_metadata(client, resource_store, monkeypatch):
    from app import cloud_computer, conversations, shared_resources
    from app.llm import LLMUsage, ToolResponse
    from test_conversations import turn
    from test_conversation_streaming import context_fixture, compact
    http, db, conversation, provider = context_fixture(client, length=60)
    from test_extensions import access_for
    access = access_for(http, db)
    key = shared_resources.object_key(access.workspace_id, ".skills/report/SKILL.md")
    captured = []
    def no_cloud(*args, **kwargs):
        pytest.fail("skill discovery or ordinary conversation started cloud")
    monkeypatch.setattr(cloud_computer, "submit", no_cloud)
    monkeypatch.setattr(cloud_computer, "execute", no_cloud)
    class Model:
        last_usage = None
        def complete_with_tools(self, *args, **kwargs):
            raise AssertionError("streaming entry required")
        def stream_with_tools(self, messages, tools, **kwargs):
            captured.append(messages)
            self.last_usage = LLMUsage(prompt_tokens=100, completion_tokens=20)
            yield "complete", ToolResponse("确定性测试摘要与答复", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda **_: Model())
    def manifest(name):
        resource_store[0][key] = f"---\nname: {name}\ndescription: 元数据用途\n---\nUNLOADED_PRIVATE_BODY".encode()
    manifest("第一版方法")
    events, payload = turn(http, conversation, "普通消息")
    assert events[-1][1]["status"] == "completed", events[-1]
    assert "第一版方法" in captured[-1][0]["content"] and "UNLOADED_PRIVATE_BODY" not in str(captured[-1])
    count = len(captured)
    # SSE reconnect reuses the durable request rather than starting a new model or cloud call.
    reconnect = http.get(f"/conversations/{conversation}/requests/{payload['request_id']}/events")
    assert reconnect.status_code == 200 and len(captured) == count
    manifest("第二版方法")
    assert compact(http, conversation, provider)[-1][1]["status"] == "completed"
    events, _ = turn(http, conversation, "压缩后继续")
    assert events[-1][1]["status"] == "completed", events[-1]
    current_system = captured[-1][0]["content"]
    assert "第二版方法" in current_system and "第一版方法" not in current_system
    assert '.skills/report/SKILL.md' in current_system and "UNLOADED_PRIVATE_BODY" not in current_system
    resource_store[0].pop(key)
    events, _ = turn(http, conversation, "删除后继续")
    assert events[-1][1]["status"] == "completed" and "第二版方法" not in captured[-1][0]["content"]
