"""Real tool/business/Agent paths with synthetic suppliers and no child queue."""
import base64
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import auth, cloud_computer as cloud, conversations, image_adapters, image_tasks, image_tools
from app.agent import AgentCancelled
from app.extensions import ExtensionRuntime
from app.models import (AgentToolExecution, ChatMessage, Conversation, CreditAccount, ImageTask,
    UsageEvent)
from auth_helpers import configured_model
from test_cloud_computer import enable_execution
from test_cloud_computer_mount import configure_mount
from cloud_command_fixtures import LocalFileCloud
from test_conversations import create, turn
from test_image_tasks import configure, png
from test_resources import access_for, resource_store


def invocation(db, access, *, name="generate_image", request=None, conversation=None):
    request = request or uuid4()
    if conversation is None:
        conversation = Conversation(workspace_id=access.workspace_id, owner_id=access.user_id)
        db.add(conversation); db.flush()
    conversation.active_request_id = request
    model_item_id, call_id = uuid4(), uuid4().hex
    tool = AgentToolExecution(id=uuid5(model_item_id, call_id), conversation_id=conversation.id, request_id=request,
        model_item_id=model_item_id, call_id=call_id, name=name)
    db.add(tool); db.commit()
    return tool, conversation


@pytest.fixture
def inline(accounts, monkeypatch, resource_store):
    http, db, _ = configure(accounts, monkeypatch)
    actor = access_for(http, db)
    tool, conversation = invocation(db, actor)
    runtime = SimpleNamespace(factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        access=actor, request_id=tool.request_id)
    attempts = []
    def generate(*args):
        attempts.append(args)
        return [(png(), "image/png")], "synthetic-inline", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    def no_dispatch(*args, **kwargs):
        pytest.fail("Agent 工具不得派发子任务")
    monkeypatch.setattr(image_tasks, "dispatch", no_dispatch)
    monkeypatch.setattr(cloud, "dispatch", no_dispatch)
    return http, db, actor, tool, conversation, runtime, attempts


def test_image_returns_saved_result_progress_and_per_invocation_idempotency(inline):
    http, db, actor, tool, conversation, runtime, attempts = inline
    progress = []
    def execute(identifier, prompt="配图"):
        return image_tools.execute(runtime, "generate_image", {"prompt": prompt},
            agent_tool_execution_id=identifier, on_progress=progress.append)
    first = execute(tool.id)
    assert first["status"] == "completed" and first["terminal"]
    assert first["files"][0]["version"] == 1
    assert http.get(first["files"][0]["download_url"]).content == png()
    assert [p["image_task"]["status"] for p in progress] == ["queued", "running", "saving", "completed"]
    assert execute(tool.id)["task_id"] == first["task_id"]
    assert execute(tool.id, "不同内容")["status_code"] == 409
    second_tool, _ = invocation(db, actor, request=runtime.request_id, conversation=conversation)
    assert execute(second_tool.id)["task_id"] != first["task_id"]
    assert len(attempts) == 2
    assert "get_image_task" not in {item["function"]["name"] for item in image_tools.tools(db, actor)}
    assert len(db.scalars(select(UsageEvent).where(UsageEvent.capability == "image_generation")).all()) == 2


def test_status_recovery_and_wrong_worker_never_claim_bound_task(inline):
    http, db, actor, tool, _, runtime, attempts = inline
    created = image_tasks.create(db, actor, image_tasks.ImageInput(prompt="配图"), str(tool.id),
        agent_tool_execution_id=tool.id)
    identifier = UUID(created["task_id"])
    assert http.get(f"/image-tasks/{identifier}").status_code == 200
    assert http.get(f"/image-tasks/by-request/{tool.id}").status_code == 200
    image_tasks.recover(runtime.factory)
    image_tasks.run(identifier, runtime.factory)
    image_tasks.run(identifier, runtime.factory, agent_tool_execution_id=uuid4())
    db.expire_all()
    assert db.get(ImageTask, identifier).status == "queued" and not attempts
    image_tasks.run(identifier, runtime.factory, agent_tool_execution_id=tool.id)
    assert len(attempts) == 1


def test_stop_before_submission_releases_hold_without_generation(inline):
    http, db, actor, tool, conversation, runtime, attempts = inline
    before = db.get(CreditAccount, actor.workspace_id).balance
    def stopped():
        raise AgentCancelled()
    result = image_tools.execute(runtime, "generate_image", {"prompt": "配图"},
        agent_tool_execution_id=tool.id, check_active=stopped)
    db.expire_all()
    assert result["status"] == "cancelled" and not attempts
    assert db.get(CreditAccount, actor.workspace_id).balance == before


def test_stop_during_generation_keeps_actual_bytes_and_charges_once(inline, monkeypatch):
    http, db, actor, tool, conversation, runtime, attempts = inline
    db.add(ChatMessage(conversation_id=conversation.id, sequence=0, role="assistant",
        request_id=runtime.request_id, status="streaming", content=""))
    db.commit()
    before = db.get(CreditAccount, actor.workspace_id).balance
    def generate(*args):
        attempts.append(args)
        stopped = http.post(f"/conversations/{conversation.id}/stop",
            json={"request_id": str(runtime.request_id)})
        assert stopped.status_code == 200, stopped.text
        return [(png(), "image/png")], "already-submitted", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    result = image_tools.execute(runtime, "generate_image", {"prompt": "配图"},
        agent_tool_execution_id=tool.id)
    assert result["status"] == "cancelled" and result["files"]
    assert http.get(result["files"][0]["download_url"]).content == png()
    db.expire_all()
    assert db.get(CreditAccount, actor.workspace_id).balance == before - Decimal("3.25")
    image_tasks.run(result["task_id"], runtime.factory)
    assert len(attempts) == 1
    assert db.get(Conversation, conversation.id).active_request_id is None


def test_lost_owner_cancels_only_unsubmitted_work(inline):
    _, db, actor, tool, conversation, runtime, attempts = inline
    first = image_tasks.create(db, actor, image_tasks.ImageInput(prompt="配图"), str(tool.id),
        agent_tool_execution_id=tool.id)
    conversation.active_request_id = None
    db.commit()
    image_tasks.recover(runtime.factory)
    db.expire_all()
    assert db.get(ImageTask, UUID(first["task_id"])).status == "cancelled"
    assert not attempts


def test_lost_submitted_image_is_uncertain_and_keeps_its_hold(inline):
    _, db, actor, tool, conversation, runtime, attempts = inline
    task = image_tasks.create(db, actor, image_tasks.ImageInput(prompt="配图"), str(tool.id),
        agent_tool_execution_id=tool.id)
    row = db.get(ImageTask, UUID(task["task_id"]))
    row.status = "running"
    row.updated_at = auth.now() - timedelta(seconds=row.snapshot["timeout"] + 301)
    conversation.active_request_id = None
    db.commit()
    image_tasks.recover(runtime.factory)
    db.expire_all()
    row = db.get(ImageTask, row.id)
    assert row.status == "uncertain" and not row.settled
    image_tasks.run(row.id, runtime.factory, agent_tool_execution_id=tool.id)
    assert not attempts


def test_cloud_bound_job_rejects_other_runners_and_cancels_orphan(inline, monkeypatch):
    _, db, actor, _, conversation, runtime, _ = inline
    fake, _ = enable_execution(db, actor, monkeypatch)
    monkeypatch.setattr(cloud, "dispatch", lambda *_: pytest.fail("不得重新派发"))
    tool, _ = invocation(db, actor, name="run_command", request=runtime.request_id, conversation=conversation)
    command = cloud.submit(db, actor, conversation.id, cloud.tool_request_id(runtime.request_id, "once"),
        {"command": "echo synthetic"}, agent_tool_execution_id=tool.id, dispatch_job=False)
    cloud.run(command.id, runtime.factory)
    cloud.run(command.id, runtime.factory, agent_tool_execution_id=uuid4())
    cloud.maintenance(runtime.factory)
    assert fake.creates == fake.starts == 0
    conversation.active_request_id = None
    db.commit()
    cloud.maintenance(runtime.factory)
    db.expire_all()
    assert db.get(cloud.Command, command.id).status == "cancelled"
    assert db.get(cloud.Computer, command.session_id).ended_at
    assert fake.creates == fake.starts == 0


def test_edit_uses_saved_reference_and_receipt_recovery_does_not_generate(inline):
    _, db, actor, tool, conversation, runtime, attempts = inline
    result = image_tools.execute(runtime, "generate_image", {"prompt": "配图"},
        agent_tool_execution_id=tool.id)
    edit, _ = invocation(db, actor, name="edit_image", request=runtime.request_id, conversation=conversation)
    reference = result["files"][0]
    edited = image_tools.execute(runtime, "edit_image",
        {"prompt": "调整配图", "references": [{"id": reference["id"], "version": reference["version"]}]},
        agent_tool_execution_id=edit.id)
    assert edited["status"] == "completed" and len(attempts[1][-1]) == 1
    from app.effective_context import ContextManager
    receipt = ContextManager._business_receipt(SimpleNamespace(access=actor, conversation_id=conversation.id),
        db, {"id": str(edit.model_item_id), "request_id": str(runtime.request_id)},
        {"id": edit.call_id, "function": {"name": "edit_image"}})
    assert receipt == edited and len(attempts) == 2


@pytest.mark.parametrize("failure", ["uncertain", "save_failed", "partial", "failed"])
def test_image_terminal_failures_are_truthful_and_never_regenerated(inline, monkeypatch, failure):
    _, db, actor, tool, _, runtime, attempts = inline
    def generate(*args):
        attempts.append(args)
        if failure == "uncertain":
            raise image_adapters.ImageUncertain("lost")
        if failure == "failed":
            raise image_adapters.ImageRejected("请求被拒绝")
        return ([] if failure == "partial" else [(png(), "image/png")]), "synthetic", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    arguments = {"prompt": "配图"}
    if failure == "partial":
        from app.models import ImageModelSettings, SiteModelProvider
        settings = db.get(ImageModelSettings, 1)
        model = db.get(SiteModelProvider, settings.generate_model_id)
        model.image_config = {**model.image_config, "max_count": 2}
        db.commit()
        arguments["count"] = 2
        monkeypatch.setattr(image_adapters, "generate", lambda *a:
            (attempts.append(a) or [(png(), "image/png")], "partial", {}))
    if failure == "save_failed":
        # Simulate a delivery failure after the verified bytes are persisted.
        def failed_save(identifier, factory):
            with factory() as work:
                work.get(ImageTask, UUID(str(identifier))).status = "save_failed"
                work.commit()
        monkeypatch.setattr(image_tasks, "save_outputs", failed_save)
    result = image_tools.execute(runtime, "generate_image", arguments,
        agent_tool_execution_id=tool.id)
    assert result["status"] == failure and bool(result.get("error")) == (failure != "partial")
    assert bool(result.get("uncertain")) == (failure == "uncertain")
    image_tools.execute(runtime, "generate_image", arguments, agent_tool_execution_id=tool.id)
    assert len(attempts) == 1


def test_bound_save_retry_and_orphan_save_recovery_never_regenerate(inline, monkeypatch):
    http, db, actor, tool, conversation, runtime, attempts = inline
    upload = image_tasks.save_outputs
    monkeypatch.setattr(image_tasks, "save_outputs", lambda *a: None)
    result = image_tools.execute(runtime, "generate_image", {"prompt": "配图"},
        agent_tool_execution_id=tool.id)
    identifier = UUID(result["task_id"])
    row = db.get(ImageTask, identifier, populate_existing=True)
    assert row.status == "saving" and row.settled and row.outputs[0]["encrypted_data"]
    conversation.active_request_id = None
    db.commit()
    monkeypatch.setattr(image_tasks, "save_outputs", upload)
    image_tasks.recover(runtime.factory, agent_only=True)
    db.refresh(row)
    assert row.status == "completed" and len(attempts) == 1
    # The explicit UI recovery endpoint uses the same delivery path, with no child dispatch.
    row.status = "save_failed"
    db.commit()
    response = http.post(f"/image-tasks/{identifier}/retry-save")
    assert response.status_code == 200 and response.json()["status"] == "completed"
    assert len(attempts) == 1
    assert len(db.scalars(select(UsageEvent).where(UsageEvent.request_id == identifier)).all()) == 1


def test_revoked_permission_after_submission_preserves_bytes_until_save_retry(inline, monkeypatch):
    http, db, actor, tool, _, runtime, attempts = inline
    from app.models import Membership
    def generate(*args):
        attempts.append(args)
        with runtime.factory() as work:
            work.get(Membership, (actor.workspace_id, actor.user_id)).role = "viewer"
            work.commit()
        return [(png(), "image/png")], "submitted-before-revocation", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    result = image_tools.execute(runtime, "generate_image", {"prompt": "配图"},
        agent_tool_execution_id=tool.id)
    assert result["status"] == "save_failed" and not result["files"]
    db.expire_all()
    row = db.get(ImageTask, UUID(result["task_id"]))
    assert row.outputs[0]["encrypted_data"] and row.settled
    db.get(Membership, (actor.workspace_id, actor.user_id)).role = "admin"
    db.commit()
    response = http.post(f"/image-tasks/{row.id}/retry-save")
    assert response.status_code == 200 and response.json()["files"]
    assert len(attempts) == 1


@pytest.mark.parametrize("executor", ["direct", "celery"])
def test_single_worker_generation_visual_read_document_and_replay(accounts, monkeypatch, resource_store, tmp_path, executor):
    """The real durable turn completes serially; neither tool gets a child slot."""
    http, db, _ = configure(accounts, monkeypatch)
    actor = access_for(http, db)
    binding = configured_model(db, actor.workspace_id)
    from app.models import SiteModelProvider
    db.get(SiteModelProvider, binding.site_provider_id).supports_images = True
    db.commit()
    enable_execution(db, actor, monkeypatch)
    from test_cloud_computer_mount import MemoryMount
    mounted = MemoryMount()
    mounted.objects = resource_store[0]
    configure_mount(db, monkeypatch, mounted)
    fake = LocalFileCloud(tmp_path / "workspace", mounted,
        f"workspaces/{actor.workspace_id}/resource-directory/")
    start_file = fake.start_file
    monkeypatch.setattr(fake, "start_file", lambda sandbox, identifier, operation:
        start_file(sandbox, identifier, {**operation,
            "path": operation["path"].removeprefix("/home/user/workspace/")}))
    def read_image(sandbox, path, *, max_bytes, check_active):
        check_active()
        raw = (fake.root / path.removeprefix("/home/user/workspace/")).read_bytes()
        assert len(raw) <= max_bytes
        return raw
    monkeypatch.setattr(fake, "read_file", read_image, raising=False)
    monkeypatch.setattr(cloud, "adapter", lambda *_: fake)
    def no_child(*a, **k):
        pytest.fail("单执行槽验收禁止工具二次派发")
    monkeypatch.setattr(image_tasks, "dispatch", no_child)
    monkeypatch.setattr(cloud, "dispatch", no_child)
    generations, calls = [], []
    monkeypatch.setattr(image_adapters, "generate", lambda *a:
        (generations.append(a) or [(png(), "image/png")], "synthetic", {}))
    encoded = base64.b64encode(png()).decode()
    document = f'<!doctype html><html lang="zh"><title>配图报告</title><h1>配图报告</h1><img alt="配图" src="data:image/png;base64,{encoded}"></html>'
    from auth_helpers import StreamingTestProvider
    from app.llm import ToolCall, ToolResponse
    class Provider(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, **kwargs):
            calls.append(messages)
            observations = [m for m in messages if m["role"] == "tool"]
            assert "get_image_task" not in {t["function"]["name"] for t in tools}
            if not observations:
                return ToolResponse(None, [ToolCall("make", "generate_image", {"prompt": "报告配图"})])
            result = json.loads(observations[-1]["content"])
            if observations[-1]["name"] == "generate_image":
                assert result["status"] == "completed" and result["files"]
                return ToolResponse(None, [ToolCall("see", "read_image", {"path": result["files"][0]["path"]})])
            if observations[-1]["name"] == "read_image":
                from test_context_image_completion import image_bytes
                assert png() in image_bytes(messages)
                return ToolResponse(None, [ToolCall("write", "write_file", {"path": "reports/report.html", "content": document})])
            assert result["path"] == "reports/report.html"
            return ToolResponse("配图已生成并写入报告。", [])
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda **k: Provider())
    conversation = create(http)
    if executor == "celery":
        from celery import Celery
        from celery.contrib.testing.app import setup_default_app
        from celery.contrib.testing.worker import start_worker
        from app import conversation_jobs, main, tasks
        import test_conversations
        # Use the production task body in an actual single-slot worker, while
        # keeping its broker/results isolated from every developer service.
        queue = Celery("issue105", broker="memory://", backend="cache+memory://", set_as_current=False)
        queue.conf.update(task_serializer="json", result_serializer="json",
            task_default_queue=f"issue105-{uuid4().hex}", worker_prefetch_multiplier=1)
        execute = queue.task(name="issue105.execute_conversation")(tasks.execute_conversation.run)
        run = conversation_jobs.run
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        monkeypatch.setattr(conversation_jobs, "run", lambda identifier:
            run(identifier, factory=factory, retrieve=main.retrieve_knowledge_base))
        executed = []
        def worker_response(response):
            identifier = response.json()["request_id"]
            execute.delay(identifier).get(timeout=30, disable_sync_subtasks=False)
            executed.append(identifier)
            return http.get(f"/conversations/{conversation['id']}/requests/{identifier}/events")
        monkeypatch.setattr(test_conversations, "generation_response", worker_response)
        with setup_default_app(queue), start_worker(queue, concurrency=1, pool="solo", perform_ping_check=False):
            events, payload = turn(http, conversation["id"], "生成配图并插入报告")
        assert executed == [payload["request_id"]]
    else:
        events, payload = turn(http, conversation["id"], "生成配图并插入报告")
    assert events[-1][1]["status"] == "completed", events[-1]
    assert len(calls) == 4 and len(generations) == 1
    progress = [data for name, data in events if name == "tool_started" and data.get("image_task")]
    assert progress and progress[0]["image_task"]["status"] == "queued"
    assert all(event["call_id"] == progress[0]["call_id"] for event in progress)
    result = next(data for name, data in events if name == "tool_result" and data["name"] == "write_file")
    assert result["command_id"] and result["cloud_computer"]["status"] == "ready"
    saved = http.get(f"/resources/{result['files'][0]['id']}/download")
    assert saved.content.decode() == document
    from html.parser import HTMLParser
    sources = []
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                sources.append(dict(attrs)["src"])
    Images().feed(saved.text)
    assert base64.b64decode(sources[0].split(",", 1)[1]) == png()
    assert http.get(f"/conversations/{conversation['id']}/requests/{payload['request_id']}/events").status_code == 200
    db.expire_all()
    assert all(t.encrypted_result for t in db.scalars(select(AgentToolExecution)))
    assert all(c.agent_tool_execution_id for c in db.scalars(select(cloud.Command)))
    assert len(generations) == 1


def test_durable_turn_stop_keeps_late_result_without_another_model_call(accounts, monkeypatch, resource_store):
    http, db, _ = configure(accounts, monkeypatch)
    actor = access_for(http, db)
    configured_model(db, actor.workspace_id)
    from auth_helpers import StreamingTestProvider
    from app.llm import ToolCall, ToolResponse
    calls = []
    class Provider(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, **kwargs):
            calls.append(messages)
            assert len(calls) == 1, "停止后不得再次调用模型"
            return ToolResponse(None, [ToolCall("make", "generate_image", {"prompt": "配图"})])
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", lambda **k: Provider())
    conversation = create(http)
    def generate(*args):
        with sessionmaker(bind=db.get_bind())() as work:
            tool = work.scalar(select(AgentToolExecution).where(
                AgentToolExecution.conversation_id == UUID(conversation["id"])))
            request_id = tool.request_id
        response = http.post(f"/conversations/{conversation['id']}/stop", json={"request_id": str(request_id)})
        assert response.status_code == 200
        return [(png(), "image/png")], "submitted-before-stop", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    events, _ = turn(http, conversation["id"], "生成配图")
    assert events[-1][1]["status"] == "cancelled"
    assert len(calls) == 1
    db.expire_all()
    tool = db.scalar(select(AgentToolExecution).where(AgentToolExecution.conversation_id == UUID(conversation["id"])))
    assert tool.encrypted_result and tool.status == "cancelled"
    message = http.get(f"/conversations/{conversation['id']}").json()["messages"][-1]
    image = next(item["image_task"] for item in message["tool_events"] if item.get("image_task"))
    assert image["status"] == "cancelled" and image["files"]
    assert http.get(image["files"][0]["download_url"]).content == png()
