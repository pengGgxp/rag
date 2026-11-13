import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import auth, cloud_computer as cloud
from app.agent import AgentCancelled, run_retrieval_agent
from app.extensions import ExtensionRuntime
from app.llm import ChatProviderConfig, ToolCall, ToolResponse
from app.models import ChatMessage, CreditAccount, CreditLedgerEntry, LoginSession, UsageEvent
from cloud_command_fixtures import LocalFileCloud
from test_cloud_computer import setup
from test_cloud_computer_mount import MemoryMount, configure_mount
from test_resources import resource_store, upload


@pytest.fixture
def runtime(accounts, monkeypatch, resource_store, tmp_path):
    client, db, conversation, access, _, factory = setup(accounts, monkeypatch)
    storage = MemoryMount()
    storage.objects = resource_store[0]
    configure_mount(db, monkeypatch, storage)
    item = upload(client, "count.txt", b"quantity 17\n")
    prefix = f"workspaces/{access.workspace_id}/resource-directory/"
    provider = LocalFileCloud(tmp_path / "workspace", storage, prefix)
    monkeypatch.setattr(cloud, "adapter", lambda *_: provider)
    monkeypatch.setattr(cloud, "dispatch", lambda identifier: cloud.run(identifier, factory))
    request_id = uuid4()
    message = ChatMessage(conversation_id=conversation.id, sequence=1, role="assistant",
        content="", request_id=request_id)
    db.add(message); db.commit()
    def extensions(provider_config=None):
        return ExtensionRuntime(factory, access, message_id=message.id, request_id=request_id,
            provider_config=provider_config)
    return client, db, conversation, access, factory, provider, item, extensions, request_id


def test_agent_read_patch_identical_read_executes_new_call_in_same_answer(runtime):
    client, db, conversation, access, factory, fake, item, extensions, request_id = runtime
    seen = []
    class Provider:
        step = 0
        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.step += 1
            if self.step > 1:
                seen.append(json.loads(messages[-1]["content"]))
            if self.step == 4:
                return ToolResponse("数量已从 17 更新为 23。", [])
            name, arguments = ("apply_patch", {"path": item["path"],
                "patch": "@@ -1 +1,2 @@\n-quantity 17\n+quantity 23\n+checked\n"}) if self.step == 2 else (
                    "read_file", {"id": item["id"]})
            # Some providers reuse IDs across rounds; the round must be part of identity.
            return ToolResponse(None, [ToolCall("reused-provider-id", name, arguments)])
    def forbidden(*args, **kwargs):
        pytest.fail("unrelated knowledge retrieval")
    result = run_retrieval_agent(query="修改并重新读取", knowledge_base_ids=[], history=[],
        factory=factory, retrieve=forbidden, access=access, provider=Provider(), extensions=extensions())
    assert result.answer == "数量已从 17 更新为 23。"
    assert seen[0]["content"] == "quantity 17\n"
    assert seen[2]["content"] == "quantity 23\nchecked\n"
    # HEAD refresh may intentionally clear an unknown hash; it must never claim the old hash.
    digest = hashlib.sha256(seen[2]["content"].encode()).hexdigest()
    assert seen[2]["sha256"] in ("", digest)
    assert seen[1]["sha256"] == digest
    assert seen[2]["size_bytes"] == len(seen[2]["content"].encode())
    assert client.get(f"/resources/{item['id']}/download").content == seen[2]["content"].encode()
    commands = db.scalars(select(cloud.Command).order_by(cloud.Command.created_at)).all()
    assert len(commands) == fake.starts == 3 and fake.creates == 1
    assert len({command.request_id for command in commands}) == 3
    assert commands[0].digest == commands[2].digest
    assert commands[0].id != commands[2].id


def test_runtime_recreation_and_worker_replay_preserve_command_and_ledger(runtime):
    client, db, conversation, access, factory, fake, item, extensions, _ = runtime
    first = extensions().execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")
    db.expire_all()
    ledger = [(row.id, row.amount) for row in db.scalars(select(CreditLedgerEntry))]
    second = extensions().execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")
    assert second["command_id"] == first["command_id"]
    cloud.run(first["command_id"], factory)
    assert fake.creates == fake.starts == 1
    db.expire_all()
    assert [(row.id, row.amount) for row in db.scalars(select(CreditLedgerEntry))] == ledger
    assert extensions().execute("run_command", {"command": "echo changed"}, call_id="tool-1-run")["error"]
    assert fake.starts == 1
    computer = db.scalar(select(cloud.Computer))
    computer.started_at = computer.last_activity_at = auth.now() - timedelta(seconds=60)
    db.commit()
    client.post(f"/conversations/{conversation.id}/cloud-computer/end")
    cloud.maintenance(factory)
    db.expire_all()
    balance = db.get(CreditAccount, access.workspace_id).balance
    ledger = [row.id for row in db.scalars(select(CreditLedgerEntry))]
    events = [row.id for row in db.scalars(select(UsageEvent))]
    assert extensions().execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")["command_id"] == first["command_id"]
    cloud.maintenance(factory)
    db.expire_all()
    assert db.get(CreditAccount, access.workspace_id).balance == balance
    assert [row.id for row in db.scalars(select(CreditLedgerEntry))] == ledger
    assert [row.id for row in db.scalars(select(UsageEvent))] == events
    assert fake.creates == fake.starts == 1


def test_identical_commands_in_one_model_response_have_distinct_invocations(runtime):
    _, db, _, access, factory, fake, _, extensions, _ = runtime
    class Provider:
        step = 0
        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.step += 1
            return ToolResponse(None, [ToolCall("first", "run_command", {"command": "echo synthetic"}),
                ToolCall("second", "run_command", {"command": "echo synthetic"})]) if self.step == 1 else (
                    ToolResponse("两次命令均已执行。", []))
    result = run_retrieval_agent(query="执行两次", knowledge_base_ids=[], history=[], factory=factory,
        retrieve=lambda *_: pytest.fail("unexpected retrieval"), access=access, provider=Provider(), extensions=extensions())
    assert result.answer == "两次命令均已执行。"
    commands = db.scalars(select(cloud.Command)).all()
    assert len(commands) == fake.starts == 2 and fake.creates == 1
    assert commands[0].digest == commands[1].digest
    assert commands[0].request_id != commands[1].request_id


@pytest.mark.parametrize("scope", ["user", "workspace", "conversation"])
def test_invocation_replay_cannot_bypass_access_scope(runtime, scope):
    _, _, conversation, access, factory, fake, _, extensions, request_id = runtime
    extensions().execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")
    checked = auth.Access(uuid4() if scope == "user" else access.user_id,
        uuid4() if scope == "workspace" else access.workspace_id, access.role, access.session_hash)
    with factory() as db, pytest.raises(HTTPException) as error:
        cloud.submit(db, checked, uuid4() if scope == "conversation" else conversation.id,
            cloud.tool_request_id(request_id, "tool-1-run"), {"command": "echo synthetic"})
    assert error.value.status_code in (403, 404) and fake.starts == 1


@pytest.mark.parametrize("status", ["queued", "preparing", "running", "completed", "cancelled", "uncertain"])
def test_same_call_conflicting_parameters_never_dispatches(runtime, monkeypatch, status):
    _, db, conversation, access, _, fake, _, _, request_id = runtime
    dispatches = []
    monkeypatch.setattr(cloud, "dispatch", dispatches.append)
    key = cloud.tool_request_id(request_id, "tool-1-run")
    command = cloud.submit(db, access, conversation.id, key, {"command": "echo first"})
    command.status = status
    db.commit()
    with pytest.raises(HTTPException) as error:
        cloud.submit(db, access, conversation.id, key, {"command": "echo second"})
    assert error.value.status_code == 409
    assert dispatches == [command.id] and fake.starts == 0
    assert cloud.submit(db, access, conversation.id, key, {"command": "echo first"}).id == command.id
    assert len(dispatches) == (2 if status == "queued" else 1)


@pytest.mark.parametrize("tool", ["read_image", "start_preview"])
def test_image_and_preview_forward_stable_invocation_identity(runtime, monkeypatch, tool):
    _, _, _, _, _, fake, _, extensions, request_id = runtime
    from app.config import get_settings
    from app.image_reading import workspace_image
    from test_chat_attachments import PNG
    captured = []
    if tool == "read_image":
        image = workspace_image("/home/user/workspace/chart.png", "image/png", 1, 1,
            hashlib.sha256(PNG).hexdigest(), PNG)
        def execute_image(db, access, conversation, request, path, check):
            captured.append(request)
            return image, {}
        monkeypatch.setattr(cloud, "execute_image", execute_image)
        config = ChatProviderConfig("https://example.test/v1", "test", "vision", supports_images=True)
        arguments = {"path": "chart.png"}
    else:
        monkeypatch.setattr(get_settings(), "preview_enabled", True)
        def execute(db, access, conversation, request, args, check):
            captured.append(request)
            return {"status": "completed", "preview": {"status": "ready"}}
        monkeypatch.setattr(cloud, "execute", execute)
        config = None
        arguments = {"command": "python -m http.server 8080", "port": 8080, "name": "验收页面"}
    for call_id in ("tool-1-call", "tool-2-call", "tool-1-call"):
        result = extensions(config).execute(tool, arguments, call_id=call_id)
        assert not isinstance(result, dict) or "error" not in result
    assert captured == [cloud.tool_request_id(request_id, key) for key in
        ("tool-1-call", "tool-2-call", "tool-1-call")]
    assert captured[0] != captured[1] and fake.starts == 0


def test_uncertain_call_is_never_resubmitted_after_runtime_recreation(runtime, monkeypatch):
    _, _, _, _, _, fake, _, extensions, _ = runtime
    def lost(*args):
        fake.starts += 1
        raise TimeoutError()
    monkeypatch.setattr(fake, "start", lost)
    first = extensions().execute("run_command", {"command": "echo side-effect"}, call_id="tool-1-run")
    assert first["uncertain"]
    second = extensions().execute("run_command", {"command": "echo side-effect"}, call_id="tool-1-run")
    assert second["uncertain"] and first["command_id"] == second["command_id"]
    assert fake.creates == fake.starts == 1


def test_cancelled_call_is_not_reexecuted(runtime, monkeypatch):
    _, db, conversation, access, factory, fake, _, _, request_id = runtime
    monkeypatch.setattr(cloud, "dispatch", lambda _: None)
    key = cloud.tool_request_id(request_id, "tool-1-run")
    def stopped():
        raise AgentCancelled()
    with pytest.raises(AgentCancelled):
        cloud.execute(db, access, conversation.id, key, {"command": "echo synthetic"}, stopped)
    command = db.scalar(select(cloud.Command))
    cloud.run(command.id, factory)
    result = cloud.execute(db, access, conversation.id, key, {"command": "echo synthetic"}, None)
    assert result["command_id"] == str(command.id)
    db.refresh(command)
    assert command.status == "cancelled" and command.cancel_requested
    assert fake.creates == fake.starts == 0


def test_revoked_session_cannot_replay_completed_command(runtime):
    _, db, _, access, _, fake, _, extensions, _ = runtime
    saved = extensions()
    saved.execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")
    db.delete(db.get(LoginSession, access.session_hash)); db.commit()
    with pytest.raises(HTTPException) as error:
        saved.execute("run_command", {"command": "echo synthetic"}, call_id="tool-1-run")
    assert error.value.status_code == 403 and fake.starts == 1


@pytest.mark.parametrize("request_id,call_id", [(None, "tool-1-x"), (uuid4(), None), (uuid4(), " ")])
def test_missing_stable_identity_is_rejected(request_id, call_id):
    with pytest.raises(HTTPException) as error:
        cloud.tool_request_id(request_id, call_id)
    assert error.value.status_code == 422
