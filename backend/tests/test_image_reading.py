import base64
import hashlib
import json
import struct
import zlib
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import cloud_computer as cloud
from app import image_reading
from app.agent import stream_retrieval_agent
from app.chat_attachments import model_messages, summary_content
from app.cloud_file_operation import inspect_image, perform
from app.extensions import ExtensionRuntime
from app.llm import ChatProviderConfig
from app.llm import OpenAICompatibleChatProvider
from app.models import Membership
from test_chat_attachments import PNG
from test_agent import runtime as agent_runtime
from test_conversation_streaming import upstream, sse
from test_extensions import access_for as load_access
from test_shared_providers import setup_site
from test_cloud_computer import FakeCloud
from test_resources import access_for
from app.llm import ToolCall, ToolResponse


JPEG = (b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        b"\xff\xd9")
WEBP = bytes.fromhex(
    "524946463c000000574542505650382030000000d001009d012a0200030001402625a0"
    "0274ba01f80003b000fef2eb7ffcd815cd73eff7ffd2e0fd2e0fd2e0ffd2900000"
)


def test_formats_are_identified_from_content_and_validated(monkeypatch):
    assert image_reading.image_type(PNG)[0] == "image/png"
    assert image_reading.image_type(JPEG)[0] == "image/jpeg"
    assert image_reading.image_type(WEBP)[0] == "image/webp"
    for data in (b"not-an-image", PNG[:-2], b"\x89PNG\r\n\x1a\n" + b"\x00" * 20):
        with pytest.raises(ValueError):
            image_reading.image_type(data)
    with pytest.raises(ValueError, match="10 MiB"):
        image_reading.image_type(PNG + b"\x00" * (10 * 1024 * 1024))
    def png_chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    fake_width = (PNG[:8] + png_chunk(b"IHDR", struct.pack(
        ">IIBBBBB", 0xFFFFFFFF, 0xFFFFFFFF, 8, 2, 0, 0, 0)) + PNG[33:])
    with pytest.raises(ValueError, match="尺寸"):
        image_reading.image_type(fake_width)


def test_local_image_paths_follow_filesystem_resolution(tmp_path):
    root = tmp_path/"workspace"
    root.mkdir()
    (root/"relative.png").write_bytes(PNG)
    (tmp_path/"outside.png").write_bytes(PNG)
    (root/".ssh").mkdir()
    (root/".ssh"/"credentials.png").write_bytes(PNG)
    (root/"link.png").symlink_to(root/"relative.png")
    (root/"directory").mkdir()
    assert inspect_image("relative.png", root)["size_bytes"] == len(PNG)
    assert inspect_image("link.png", root)["size_bytes"] == len(PNG)
    assert inspect_image(".ssh/credentials.png", root)["size_bytes"] == len(PNG)
    assert inspect_image("../outside.png", root)["size_bytes"] == len(PNG)
    assert image_reading.local_path("../outside.png") == "/home/user/workspace/../outside.png"
    assert image_reading.local_path("/tmp/private.pem") == "/tmp/private.pem"
    with pytest.raises(ValueError, match="普通文件"):
        inspect_image("directory", root)
    with pytest.raises(FileNotFoundError):
        inspect_image("missing.png", root)


def test_local_image_change_is_detected(monkeypatch, tmp_path):
    path = tmp_path/"image.png"
    path.write_bytes(PNG)
    original = Path.stat
    calls = {"count": 0}
    def changed_stat(target, *args, **kwargs):
        value = original(target, *args, **kwargs)
        calls["count"] += 1
        return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev, st_ino=value.st_ino,
            st_size=value.st_size + (1 if calls["count"] > 1 else 0),
            st_mtime_ns=value.st_mtime_ns)
    monkeypatch.setattr(Path, "stat", changed_stat)
    with pytest.raises(RuntimeError, match="变化"):
        perform({"name": "inspect_image", "path": path.name}, tmp_path)


def test_model_capability_and_arguments_are_checked_before_io():
    called = []
    arguments = ({"url": "https://example.com/chart.png"}, {},
                 {"path": "a.png", "url": "https://example.com/a.png"},
                 {"path": "a.png", "extra": True})
    config = ChatProviderConfig("https://example.test/v1", "test", "vision", supports_images=True)
    for value in arguments:
        assert image_reading.read_image(value, config=config,
            local_loader=lambda path: called.append(path)).get("error")
    assert not called
    accepted = []
    result = image_reading.read_image({"path": "../.ssh/credentials.png"}, config=config,
        local_loader=lambda path: (accepted.append(path) or PNG, {
            "size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}))
    assert result.data == PNG
    assert accepted == ["/home/user/workspace/../.ssh/credentials.png"]
    unsupported = image_reading.read_image({"path": "chart.png"}, config=ChatProviderConfig(
        "https://example.test/v1", "test", "text", supports_images=False),
        local_loader=lambda path: called.append(path))
    assert unsupported["error_code"] == "image_model_unsupported" and not called


def test_model_image_enters_followup_requests_and_summary_drops_tool_image_bytes():
    value = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    messages = [{"role": "user", "content": [value.context_part()]}]
    config = ChatProviderConfig("https://example.test/v1", "test", "vision", supports_images=True)
    for protocol in ("chat_completions", "responses", "anthropic", "gemini"):
        outbound = model_messages(messages, replace(config, api_protocol=protocol))
        part = outbound[0]["content"][0]
        assert part["type"] == "image_url"
        assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == PNG
        assert messages[0]["content"][0]["data"] == PNG
    summary = summary_content(messages)
    assert isinstance(summary, str)
    assert "按需读取过图片「chart.png」；需要查看请再次调用 read_image" in summary
    assert "model_image" not in summary
    assert base64.b64encode(PNG).decode() not in summary
    persisted = json.dumps(image_reading.persisted_model_images(messages), ensure_ascii=False, default=str)
    assert base64.b64encode(PNG).decode() not in persisted
    assert "按需读取过图片「chart.png」；需要查看请再次调用 read_image" in persisted
    assert "/home/user/workspace/chart.png" not in persisted


def test_summary_removes_models_images_without_bytes_from_serialized_text():
    messages = [{"role": "user", "content": [
        {"type": "model_image", "filename": "chart.png", "content_type": "image/png",
            "width": 1200, "height": 800}]},
    ]
    summary = summary_content(messages)
    assert isinstance(summary, str)
    assert "按需读取过图片「chart.png」；需要查看请再次调用 read_image" in summary
    assert base64.b64encode(PNG).decode() not in summary


@pytest.mark.parametrize("protocol", [
    "chat_completions", "responses", "anthropic", "gemini",
])
def test_compaction_drops_read_image_tool_payload_for_every_protocol(protocol):
    value = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "image-call",
            "function": {"name": "read_image", "arguments": '{"path":"chart.png"}'}}]},
        {"role": "tool", "tool_call_id": "image-call", "content": '{"filename":"chart.png"}'},
    ]
    if protocol in ("anthropic", "gemini"):
        messages[1]["_image_input"] = value.context_part()
    else:
        messages.append({"role": "user", "content": [value.context_part()]})

    summary = summary_content(messages)
    serialized = summary if isinstance(summary, str) else json.dumps(summary, default=str)
    assert "read_image" in serialized
    assert "chart.png" in serialized
    assert base64.b64encode(PNG).decode("ascii") not in serialized
    assert "model_image" not in serialized
    persisted = json.dumps(
        image_reading.persisted_model_images(messages), ensure_ascii=False, default=str)
    assert base64.b64encode(PNG).decode("ascii") not in persisted
    assert "/home/user/workspace/chart.png" not in persisted


def test_persisted_images_are_safe_text_and_history_has_no_bytes(monkeypatch):
    from types import SimpleNamespace
    from app import thinking
    from app.thinking import ThinkingSelection
    value = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "image",
            "function": {"name": "read_image", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "image", "content": '{"filename": "chart.png"}',
         "_image_input": value.context_part()},
        {"role": "user", "content": [value.context_part()]},
    ]
    persisted = image_reading.persisted_model_images(messages)
    serialized = json.dumps(persisted, ensure_ascii=False, default=str)
    assert base64.b64encode(PNG).decode() not in serialized
    assert "按需读取过图片「chart.png」；需要查看请再次调用 read_image" in serialized
    assert "/home/user/workspace/chart.png" not in serialized
    assert persisted[1]["_image_input"] == {"type": "text", "text":
        "按需读取过图片「chart.png」；需要查看请再次调用 read_image"}
    config = ChatProviderConfig("https://example.test/v1", "test", "vision",
        provider_id="provider", site_provider_id="provider",
        connection_id="connection", supports_images=True, api_protocol="responses",
        thinking=ThinkingSelection(adapter="none", kind="fixed"))
    monkeypatch.setattr("app.mcp_client.decrypt_headers", lambda _: {
        "continuation": json.dumps({"provider": "provider", "model": "vision",
            "protocol": "responses", "adapter": "none",
            "connection": "connection", "messages": persisted})})
    row = SimpleNamespace(role="assistant", content="回答", encrypted_continuation="encrypted")
    restored = thinking.history_message(row, config)
    assert restored[-1]["content"][0]["type"] == "text"
    assert restored[1]["_image_input"]["type"] == "text"
    assert base64.b64encode(PNG).decode() not in json.dumps(restored, default=str)
    assert "/home/user/workspace/chart.png" not in json.dumps(restored)


def test_repeated_reads_can_load_same_image_on_demand():
    value = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    config = ChatProviderConfig("https://example.test/v1", "test", "vision", supports_images=True)
    first = image_reading.read_image({"path": value.image["path"]}, config=config,
        local_loader=lambda _: (PNG, {"size_bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest()}))
    second = image_reading.read_image({"path": value.image["path"]}, config=config,
        local_loader=lambda _: (PNG, {"size_bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest()}))
    assert first.data == second.data == PNG


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_loaded_image_reaches_four_actual_wire_payloads(monkeypatch, protocol):
    value = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    messages = [{"role": "user", "content": [value.context_part()]}]
    config = ChatProviderConfig("https://provider.example/v1", "test-key", "vision",
        api_protocol=protocol, supports_images=True)
    provider = OpenAICompatibleChatProvider(config=config)
    seen = []
    def respond(handler, payload):
        seen.append(payload)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        if protocol == "chat_completions":
            sse(handler, {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
            return
        if protocol == "responses":
            sse(handler, {"type": "response.completed", "response": {
                "status": "completed", "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}]}]}})
            return
        if protocol == "anthropic":
            sse(handler, {"type": "message_start", "message": {"usage": {"input_tokens": 1}}})
            sse(handler, {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}})
            sse(handler, {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "ok"}})
            sse(handler, {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1}})
            sse(handler, {"type": "message_stop"})
            return
        sse(handler, {"candidates": [{"content": {"parts": [{"text": "ok"}]},
            "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 1,
            "candidatesTokenCount": 1}})
    with upstream(respond) as url:
        provider.config = replace(config, base_url=url)
        list(provider.stream_with_tools(messages, []))
    encoded = base64.b64encode(PNG).decode()
    assert encoded in json.dumps(seen)
    assert "test-key" not in json.dumps(seen) and "model_image" not in json.dumps(seen)


def test_runtime_exposes_read_image_only_for_supported_models(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = load_access(admin, db)
    supported = ChatProviderConfig("https://example.test/v1", "test", "vision", supports_images=True)
    unsupported = ChatProviderConfig("https://example.test/v1", "test", "text", supports_images=False)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, provider_config=supported)
    assert "read_image" in {item["function"]["name"] for item in runtime.tools}
    image_tool = next(item["function"] for item in runtime.tools
        if item["function"]["name"] == "read_image")
    assert image_tool["parameters"]["required"] == ["path"]
    assert set(image_tool["parameters"]["properties"]) == {"path"}
    text_runtime = ExtensionRuntime(lambda: nullcontext(db), access, provider_config=unsupported)
    assert "read_image" not in {item["function"]["name"] for item in text_runtime.tools}
    assert text_runtime.execute("read_image", {"path": "chart.png"})["error_code"] == "image_model_unsupported"


@pytest.mark.parametrize("protocol", ["chat_completions", "anthropic", "gemini"])
def test_agent_injects_loaded_image_only_after_tool_call(agent_runtime, protocol):
    seen = []
    image = image_reading.workspace_image(
        "/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)

    class Extensions:
        tools = []
        skills = []
        environment_description = "test"

        def label(self, name, arguments=None):
            return "查看图片"

        def execute(self, name, arguments, check_active=None):
            from app.extensions import ImageToolResult
            return ImageToolResult(image, {"filename": "chart.png",
                "summary": "已读取图片「chart.png」"})

    class Provider:
        config = SimpleNamespace(api_protocol=protocol)
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                assert not any(part.get("type") == "model_image"
                    for message in messages if isinstance(message.get("content"), list)
                    for part in message["content"])
                return ToolResponse(content=None, tool_calls=[
                    ToolCall(id="image", name="read_image", arguments={"path": "chart.png"})])
            seen.extend(messages)
            return ToolResponse(content="图片显示一张图表。", tool_calls=[])

    events = list(stream_retrieval_agent(**agent_runtime.arguments, provider=Provider(),
        extensions=Extensions()))
    if protocol in ("anthropic", "gemini"):
        tool_message = next(message for message in seen if message.get("role") == "tool")
        assert tool_message["_image_input"]["type"] == "model_image"
        assert tool_message["_image_input"]["data"] == PNG
        assert not any(part.get("type") == "model_image"
            for message in seen if isinstance(message.get("content"), list)
            for part in message["content"])
    else:
        image_parts = [part for message in seen if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "model_image"]
        assert len(image_parts) == 1 and image_parts[0]["data"] == PNG
    activity = next(data for name, data in events if name == "tool_result")
    assert activity["summary"] == "已读取图片「chart.png」"
    started = next(data for name, data in events if name == "tool_started")
    assert started["kind"] == "tool"
    assert base64.b64encode(PNG).decode() not in json.dumps(
        [(name, data) for name, data in events if name != "complete"], ensure_ascii=False)


def test_runtime_local_image_uses_cloud_computer_and_returns_safe_result(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = load_access(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, provider_config=ChatProviderConfig(
        "https://example.test/v1", "test", "vision", supports_images=True))
    image = image_reading.workspace_image("/home/user/workspace/chart.png", "image/png", 1, 1,
        hashlib.sha256(PNG).hexdigest(), PNG)
    calls = []

    def execute_image(db, access, conversation, request, path, check):
        calls.append((conversation, path, check))
        return image, {"cloud_computer": {"status": "ready"}, "command_id": "command"}

    monkeypatch.setattr(cloud, "execute_image", execute_image)
    result = runtime.execute("read_image", {"path": "chart.png"})
    assert calls and calls[0][1] == "/home/user/workspace/chart.png"
    assert result.image.data == PNG
    assert result.result["filename"] == "chart.png"
    assert "data" not in result.result and "base64" not in json.dumps(result.result).lower()
    assert runtime.label("read_image") == "查看图片"
    member = db.get(Membership, (access.workspace_id, access.user_id))
    member.role = "viewer"
    db.commit()
    viewer_access = load_access(admin, db)
    viewer = ExtensionRuntime(lambda: nullcontext(db), viewer_access, provider_config=ChatProviderConfig(
        "https://example.test/v1", "test", "vision", supports_images=True))
    assert "read_image" in {item["function"]["name"] for item in viewer.tools}
    assert "write_file" not in {item["function"]["name"] for item in viewer.tools}


def test_cloud_command_keeps_only_safe_image_metadata():
    class ImageCloud(FakeCloud):
        def start_file(self, sandbox, identifier, operation):
            self.starts += 1
            self.operation = operation
            return 42

        def file_result(self, sandbox, identifier):
            return {"value": {"size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}}

        def read_file(self, sandbox, path, *, max_bytes, owner="user"):
            assert max_bytes == 10 * 1024 * 1024 and owner == "user"
            return PNG

    assert ImageCloud.read_file(ImageCloud(), None, "/home/user/workspace/chart.png",
        max_bytes=10 * 1024 * 1024) == PNG


def test_workspace_image_end_to_end_uses_cloud_environment_and_no_duplicate_start(accounts, monkeypatch):
    from test_cloud_computer import enable_execution
    from test_cloud_computer_mount import MemoryMount
    from app.models import Conversation
    from app import storage_backend

    (client, _, _), db = accounts
    from test_auth import register
    register(client)
    user_access = access_for(client, db)
    fake, execute = enable_execution(db, user_access, monkeypatch)
    storage = MemoryMount()
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: storage)
    conversation = Conversation(workspace_id=user_access.workspace_id, owner_id=user_access.user_id)
    db.add(conversation)
    db.commit()
    path = "/home/user/workspace/chart.png"
    def start_file(sandbox, identifier, operation):
        fake.starts += 1
        fake.operation = operation
        return 42
    fake.start_file = start_file
    fake.file_result = lambda sandbox, identifier: {
        "value": {"size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}}
    fake.read_file = lambda sandbox, target, *, max_bytes, owner="user": PNG
    image, result = cloud.execute_image(db, user_access, conversation.id, uuid4(), path, None)
    assert image.data == PNG and image.image["width"] == image.image["height"] == 1
    assert fake.creates == 1 and fake.starts == 1
    assert result["status"] == "completed"
    assert "data" not in json.dumps(result)
    second, _ = cloud.execute_image(db, user_access, conversation.id, uuid4(), path, None)
    assert second.data == PNG and fake.creates == 1 and fake.starts == 2


def test_workspace_image_passes_paths_to_sandbox_for_access_checks(accounts, monkeypatch):
    from test_cloud_computer import enable_execution
    from test_cloud_computer_mount import MemoryMount
    from app import storage_backend
    (client, _, _), db = accounts
    from test_auth import register
    register(client)
    user_access = access_for(client, db)
    fake, _ = enable_execution(db, user_access, monkeypatch)
    storage = MemoryMount()
    monkeypatch.setattr(storage_backend, "current_storage", lambda _db: storage)
    from app.models import Conversation
    conversation = Conversation(workspace_id=user_access.workspace_id, owner_id=user_access.user_id)
    db.add(conversation)
    db.commit()
    with pytest.raises(Exception):
        cloud.execute_image(db, user_access, conversation.id, uuid4(), "/home/user/.ssh/id_rsa", None)
    assert fake.creates == 1
