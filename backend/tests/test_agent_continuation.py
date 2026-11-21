"""Agent boundaries: observations persist, individual tools fail, tasks continue."""
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agent import AgentCancelled, run_retrieval_agent
from app.agent_tools import bounded_message, update_plan
from app.context_windows import groups, make_item, project
from app.effective_context import ContextManager
from app.llm import LLMContextCapacityError, OpenAICompatibleChatProvider, ToolCall, ToolResponse
from app.models import AgentToolExecution, ChatMessage, Conversation, ConversationContext
from test_context_windows import client, setup_store
from test_context_replacement import manager_for, Summary
from test_resources import resource_store


def fresh_manager(client):
    db, store, request = setup_store(client)
    config = replace(store.config, context_capacity=64000, max_output_tokens=2048)
    manager = ContextManager(sessionmaker(bind=db.get_bind(), expire_on_commit=False), store.access,
        store.conversation_id, config, None, None, request)
    manager.history(db, db.get(Conversation, store.conversation_id)); db.commit()
    manager.current_message_id = uuid4()
    manager.start({"role": "system", "content": "system"}, "开始任务")
    return db, manager


def assistant(calls):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.serialized_arguments()}} for c in calls]}


def tool_message(call, result):
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, ensure_ascii=False)}


def restart(db, manager, protocol=None):
    request = uuid4()
    db.get(Conversation, manager.conversation_id).active_request_id = request; db.commit()
    config = replace(manager.config, api_protocol=protocol) if protocol else manager.config
    next_manager = ContextManager(manager.factory, manager.access, manager.conversation_id, config, None, None, request)
    next_manager.history(db, db.get(Conversation, manager.conversation_id)); db.commit()
    next_manager.current_message_id = uuid4()
    next_manager.start({"role": "system", "content": "new-system"}, "继续任务")
    return next_manager


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_partial_tool_group_recovers_real_success_uncertain_and_not_executed(client, protocol):
    db, manager = fresh_manager(client)
    calls = [ToolCall(str(n), "write_file", {"path": f"{n}.txt"}) for n in range(3)]
    manager.record([assistant(calls)], "model", "model:1")
    first = manager.begin_tool(calls[0])
    manager.finish_tool(first, tool_message(calls[0], {"saved": "真实成功回执"}), 2000)
    manager.begin_tool(calls[1])
    resumed = restart(db, manager, protocol)
    assert not groups(resumed.items)[1]
    results = [json.loads(i["message"]["content"]) for i in resumed.items if i["message"].get("role") == "tool"]
    assert results[0] == {"saved": "真实成功回执"}
    assert results[1]["execution_status"] == "uncertain" and results[1]["uncertain"]
    assert results[2]["execution_status"] == "not_executed" and not results[2]["uncertain"]
    assert "真实成功回执" not in db.get(AgentToolExecution, first).encrypted_result
    assert len(resumed.messages) == len(project(resumed.items, resumed.config)) + 1


def test_result_saved_after_stop_is_recovered_without_replaying_write(client, monkeypatch):
    db, manager = fresh_manager(client)
    call = ToolCall("once", "write_file", {})
    manager.record([assistant([call])], "model", "model:1")
    identifier = manager.begin_tool(call)
    db.get(Conversation, manager.conversation_id).active_request_id = None; db.commit()
    with pytest.raises(AgentCancelled):
        manager.finish_tool(identifier, tool_message(call, {"saved": True}), 2000)
    assert db.get(AgentToolExecution, identifier).status == "completed"
    resumed = restart(db, manager)
    result = next(i["message"] for i in resumed.items if i["message"].get("role") == "tool")
    assert json.loads(result["content"]) == {"saved": True}


def test_full_large_result_is_saved_and_can_be_read_in_pages(client):
    db, manager = fresh_manager(client)
    call = ToolCall("large", "read_file", {})
    manager.record([assistant([call])], "model", "model:1")
    identifier = manager.begin_tool(call)
    raw = tool_message(call, {"text": "唯一资料段落" * 10000})
    visible = manager.finish_tool(identifier, raw, 1800)
    preview = json.loads(visible["content"])
    assert preview["truncated"] and len(visible["content"]) < len(raw["content"])
    content, offset = "", 0
    while True:
        page = manager.read_result({"result_id": str(identifier), "offset": offset, "limit": 8000})
        content += page["text"]
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert content == raw["content"]
    assert manager.read_result({"result_id": str(uuid4())}).get("error")


def test_legacy_large_tool_result_can_continue_and_read_after_compact(client):
    from app.conversation_context import estimate_request, input_budget
    db, manager = fresh_manager(client)
    call = ToolCall("legacy-large", "read_file", {})
    original = tool_message(call, {"text": "旧资料完整内容" * 18000})
    # Older windows have no separate execution receipt.
    manager.record([assistant([call]), original], "model", "old-output")
    resumed = restart(db, manager)
    preview = json.loads(next(m["content"] for m in resumed.messages if m.get("role") == "tool"))
    assert preview["truncated"]
    assert estimate_request(resumed.config, resumed.messages) < input_budget(resumed.config)
    saved = resumed.read_result({"result_id": preview["result_id"], "offset": preview["next_offset"]})
    assert original["content"][saved["offset"]:].startswith(saved["text"])
    resumed.provider, resumed.manual = Summary(), True
    resumed.prepare(resumed.messages, [], lambda _: None, lambda: None)
    assert resumed.read_result({"result_id": preview["result_id"]})["text"] == original["content"][:resumed.read_result({"result_id": preview["result_id"]})["next_offset"]]


def test_rebudgeting_result_keeps_offsets_in_full_original():
    raw = {"role": "tool", "content": json.dumps({"text": "资料" * 10000}, ensure_ascii=False)}
    identifier = uuid4()
    first = bounded_message(raw, identifier, 4000)
    second = json.loads(bounded_message(first, identifier, 1000)["content"])
    assert second["preview"] == raw["content"][:second["next_offset"]]
    assert second["total_characters"] == len(raw["content"])


def test_history_text_and_attachments_have_a_continuation_page(client):
    db, manager = fresh_manager(client)
    original = "长历史内容" * 5000
    db.add(ChatMessage(conversation_id=manager.conversation_id, sequence=1, role="user", content=original)); db.commit()
    result = manager.read_original({"start_sequence": 1, "end_sequence": 1})
    assert result["next_offset"] and len(result["text"]) < len(original)
    second = manager.read_original({"start_sequence": 1, "end_sequence": 1, "offset": result["next_offset"]})
    assert second["offset"] == result["next_offset"]


def test_bad_parameters_are_tool_errors_and_model_corrects_them(client):
    db, manager = fresh_manager(client)
    captured = []
    class Model:
        def complete_with_tools(self, messages, tools, **kwargs):
            captured.append(messages)
            if len(captured) == 1:
                return ToolResponse(None, [ToolCall.parse("bad", "update_plan", "{broken")])
            if len(captured) == 2:
                assert json.loads(messages[-1]["content"])["error_code"] == "tool_arguments"
                return ToolResponse(None, [ToolCall("good", "update_plan", {"plan": [{"step": "完成", "status": "completed"}]})])
            assert json.loads(messages[-1]["content"])["plan"][0]["status"] == "completed"
            return ToolResponse("完成", [])
    run = run_retrieval_agent(query="开始任务", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager)
    assert run.answer == "完成" and len(run.tool_results) == 2
    assert manager.current_plan()["plan"][0]["status"] == "completed"


def test_plan_survives_compact_as_explicit_progress(client):
    db, manager = manager_for(client, provider=Summary())
    plan = update_plan({"plan": [{"step": "待验证资料", "status": "in_progress"}]})
    # A completed prior plan is a valid atomic tool group.
    call = ToolCall("plan", "update_plan", {})
    manager.record([assistant([call]), tool_message(call, plan)], "model", "plan", plan=plan)
    manager.record([{"role": "assistant", "content": "当前进展"}], "model", "done")
    manager.prepare(manager.messages, [], lambda _: None, lambda: None)
    assert manager.current_plan() == plan
    assert "待验证资料" in str(manager.messages)


def test_explicit_provider_capacity_retries_current_model_step_once(client):
    db, manager = manager_for(client, provider=Summary())
    manager.manual = False
    manager.config = replace(manager.config, context_capacity=200000)
    class Model:
        normal = 0
        summaries = 0
        def stream_with_tools(self, messages, tools, **kwargs):
            if not tools:
                self.summaries += 1
                yield "complete", ToolResponse("短任务交接", [])
            else:
                self.normal += 1
                if self.normal == 1:
                    raise LLMContextCapacityError("explicit capacity")
                assert "RESULT-REMOVED" not in str(messages)
                yield "complete", ToolResponse("继续完成", [])
    model = Model(); manager.provider = model
    run = run_retrieval_agent(query="CURRENT-INPUT", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=model, context_manager=manager)
    assert run.answer == "继续完成" and model.normal == 2 and model.summaries == 1


def test_catalog_changes_keep_existing_tool_results_images_and_checkpoint(client):
    from app.models import KnowledgeBase, Document, DocumentStatus
    from test_context_image_completion import PNG
    from hashlib import sha256
    db, manager = manager_for(client, provider=Summary())
    manager.prepare(manager.messages, [], lambda _: None, lambda: None)
    image_call = ToolCall("snapshot", "read_image", {})
    image_result = tool_message(image_call, {"loaded": True})
    image_result["_image_input"] = {"type": "model_image", "data": PNG, "filename": "chart.png", "sha256": sha256(PNG).hexdigest()}
    manager.record([assistant([image_call]), image_result], "model", "image")
    state, old = manager.store.load(db); db.commit()
    original_window = state.window_id
    # A digest left by v0.1.18 must not invalidate a healthy encrypted window.
    state.scope_digest = "old-catalog-digest"
    library = KnowledgeBase(account_id=manager.access.user_id, owner_id=manager.access.user_id, name="普通新增")
    db.add(library); db.flush()
    document = Document(knowledge_base_id=library.id, filename="new.txt", content_type="text/plain",
        size_bytes=1, object_key="fixture-only", content_hash="a" * 64, status=DocumentStatus.PROCESSING)
    db.add(document); db.commit()
    for change in ("processing", "ready", "rename", "delete"):
        if change == "ready":
            document.status = DocumentStatus.READY
        elif change == "rename":
            document.filename = "renamed.txt"
        elif change == "delete":
            document.status = DocumentStatus.DELETED
        db.commit()
        restored, items = manager.store.load(db); db.commit()
        assert restored.window_id == original_window and items == old


def test_unrelated_pending_file_operation_does_not_gate_observation_or_new_file(client, resource_store, monkeypatch):
    from app import file_operations as ops, shared_resources as shared
    from app.skill_discovery import summary
    from app.workspace_mutations import save_files, snapshot
    from app.models import WorkspaceFileOperation
    from fastapi import HTTPException
    db, manager = fresh_manager(client)
    key = shared.object_key(manager.access.user_id, "conflicted.txt")
    resource_store[0][key] = b"newer external bytes"
    db.add(WorkspaceFileOperation(account_id=manager.access.user_id, user_id=manager.access.user_id,
        request_id="old-op", fingerprint="a" * 64, state="recovery_required", steps=[{"key": key}], result={}))
    db.commit()
    monkeypatch.setattr(ops, "restore", lambda *_: pytest.fail("observation invoked recovery"))
    assert "conflicted.txt" in snapshot(db, manager.access)[0]
    assert summary(db, manager.access) == []
    saved = save_files(db, manager.access, {"unrelated.txt": (b"new output", "text/plain")})
    assert saved["items"][0]["path"] == "unrelated.txt"
    assert resource_store[0][key] == b"newer external bytes"
    with pytest.raises(HTTPException) as error:
        save_files(db, manager.access, {"conflicted.txt": (b"must not overwrite", "text/plain")},
            expected={"conflicted.txt": ops.file_revision(snapshot(db, manager.access)[0]["conflicted.txt"])})
    assert error.value.status_code == 409
    assert "目标文件" in str(error.value.detail)


def test_optional_skill_hints_do_not_refresh_storage_or_take_write_locks(client, monkeypatch):
    from app import shared_resources
    from app.skill_discovery import summary
    db, manager = fresh_manager(client)
    monkeypatch.setattr(shared_resources, "refresh_index", lambda *_a, **_k: pytest.fail("model admission refreshed storage"))
    assert summary(db, manager.access) == []


@pytest.mark.parametrize("code", ["context_length_exceeded", "context_window_exceeded", "prompt_too_long"])
def test_structured_capacity_error_is_classified_without_guessing_ordinary_400(code):
    import httpx
    from app.upstream_retries import context_capacity_failure
    assert isinstance(context_capacity_failure(httpx.Response(400, json={"error": {"code": code}})), LLMContextCapacityError)
    assert context_capacity_failure(httpx.Response(400, json={"error": {"message": "bad request"}})) is None
    assert context_capacity_failure(httpx.Response(503, json={"error": {"code": code}})) is None


def test_error_detail_retains_reason_and_redacts_credentials():
    from app.tool_errors import public_error
    value = public_error("上级路径是文件；api_key=abcdef token=xyz https://user:pass@example.test/path Bearer raw-secret")
    assert "上级路径是文件" in value
    assert all(secret not in value for secret in ("abcdef", "xyz", "user:pass", "raw-secret"))
