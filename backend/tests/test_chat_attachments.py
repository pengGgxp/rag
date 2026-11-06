import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import struct
import zlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import auth, chat_attachments as attachments, conversations as chat
from app.conversation_context import estimate_input
from app.llm import ChatProviderConfig, LLMConfigurationError, OpenAICompatibleChatProvider, ToolResponse
from app.models import ChatAttachment, CreditLedgerEntry, SiteModelProvider
from test_auth import accounts, register, share, switch
from test_conversations import client, create, turn
from auth_helpers import model_id

def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + png_chunk(b"IEND", b""))


@pytest.fixture
def objects(monkeypatch):
    values = {}
    monkeypatch.setattr(attachments, "upload_fileobj", lambda source, key, mime: values.update({key: source.read()}))
    monkeypatch.setattr(attachments, "download_fileobj", lambda key: io.BytesIO(values[key]))
    from app import storage_downloads
    monkeypatch.setattr(storage_downloads, "download_range_fileobj", lambda key, start, end: io.BytesIO(values[key][start:end+1]))
    from test_cloud_computer_mount import MemoryMount
    storage = MemoryMount(); storage.objects = values
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: storage)
    return values


def upload(http, filename="note.txt", data=b"Private attached facts"):
    return http.post("/chat/attachments", files={"file": (filename, data, "application/octet-stream")})


def test_attachment_ownership_expiry_and_deletion(accounts, objects):
    (owner, outsider, anonymous), db = accounts
    identity = register(owner)
    register(outsider, "outsider@example.com")
    response = upload(owner)
    assert response.status_code == 201, response.text
    item = response.json()
    assert {"id", "filename", "content_type", "size_bytes", "kind", "resource_id", "version"} <= set(item)
    assert "object_key" not in response.text and "Private attached" not in response.text
    path = f"/chat/attachments/{item['id']}"
    assert outsider.get(path + "/content").status_code == 404
    assert anonymous.get(path + "/content").status_code == 401
    assert outsider.delete(path).status_code == 404
    share(owner, "outsider@example.com", "admin")
    switch(outsider, identity["workspace_id"])
    assert outsider.get(path + "/content").status_code == 200
    result = owner.get(path + "/content")
    assert result.content == b"Private attached facts" and "no-store" in result.headers["cache-control"]
    assert owner.delete(path).status_code == 204
    assert owner.get(path + "/content").status_code == 404
    fresh = upload(owner).json()
    row = db.get(ChatAttachment, UUID(fresh["id"]))
    row.created_at = auth.now() - timedelta(hours=25)
    db.commit()
    assert owner.get(f"/chat/attachments/{row.id}/content").status_code == 200
    deleted = []
    attachments.cleanup(db, SimpleNamespace(delete=lambda key: deleted.append(key)))
    assert not deleted and len(db.scalars(select(ChatAttachment)).all()) == 2


@pytest.mark.parametrize("name,data,code", [
    ("a.exe", b"data", 415), ("a.txt", b"", 422), ("../a.txt", b"text", 415),
    ("a.png", b"not an image", 415), ("a.pdf", b"%PDF-fake", 415),
    ("a.txt", b"x" * 100_001, 413), ("a.png", PNG[:-3], 415),
    ("binary.txt", b"\x00binary", 415),
], ids=["suffix", "empty", "path", "fake-image", "fake-pdf", "text-limit", "truncated-png", "binary"])
def test_invalid_uploads_do_not_store_files(accounts, objects, name, data, code):
    (owner, _, _), db = accounts
    register(owner)
    response = upload(owner, name, data)
    assert response.status_code == code, response.text
    assert not objects and not db.scalars(select(ChatAttachment)).all()


def test_pdf_scan_no_paid_ocr_and_office_text(accounts, objects):
    from pypdf import PdfWriter
    from docx import Document
    from openpyxl import Workbook
    (owner, _, _), _db = accounts
    register(owner)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO(); writer.write(data)
    result = upload(owner, "scan.pdf", data.getvalue())
    assert result.status_code == 422 and "扫描件" in result.json()["detail"]
    document = Document(); document.add_paragraph("paragraph")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "table fact"
    data = io.BytesIO(); document.save(data)
    assert upload(owner, "facts.docx", data.getvalue()).status_code == 201
    workbook = Workbook(); workbook.active.append(["sheet fact", 123])
    data = io.BytesIO(); workbook.save(data)
    assert upload(owner, "facts.xlsx", data.getvalue()).status_code == 201


def test_model_capability_is_explicit_and_multimodal_protocols(objects):
    assert attachments.image_size(PNG, ".png") == (1, 1)
    row = SimpleNamespace(id=uuid4(), filename="image.png", kind="image", object_key="private-key",
        content_type="image/png", width=1, height=1, content_hash=hashlib.sha256(PNG).hexdigest())
    objects[row.object_key] = PNG
    messages = [{"role": "user", "content": attachments.user_content("Look", [row])}]
    config = ChatProviderConfig("https://example.com/v1", "test", "vision", supports_images=True)
    original = repr(messages)
    for protocol in ("chat_completions", "responses"):
        provider = OpenAICompatibleChatProvider(config=replace(config, api_protocol=protocol))
        payload = provider._payload(messages, stream=True)
        content = payload["messages"][0]["content"] if protocol == "chat_completions" else payload["input"][0]["content"]
        assert content[0]["type"] == ("text" if protocol == "chat_completions" else "input_text")
        image = content[-1]
        assert image["type"] == ("image_url" if protocol == "chat_completions" else "input_image")
        url = image["image_url"]["url"] if protocol == "chat_completions" else image["image_url"]
        assert base64.b64decode(url.split(",")[1]) == PNG
        assert "private-key" not in repr(payload) and "chat_image" not in repr(payload)
    assert repr(messages) == original
    assert estimate_input(messages) > 1000
    summary = attachments.summary_content(messages)
    assert summary[1]["type"] == "chat_image" and "private-key" not in summary[0]["text"]
    with pytest.raises(LLMConfigurationError, match="图片"):
        attachments.model_messages(messages, replace(config, supports_images=False))
    objects[row.object_key] = b"tampered"
    with pytest.raises(LLMConfigurationError, match="校验"):
        attachments.model_messages(messages, config)


def test_unattached_history_keeps_existing_checkpoint_digest():
    from app.conversation_context import digest, source_digest
    row = SimpleNamespace(id=uuid4(), sequence=0, role="user", content="prior message", status="completed",
        encrypted_continuation=None, citations=[], attachments=[])
    legacy = [(str(row.id), 0, "user", "prior message", "completed", None, [])]
    assert source_digest([row]) == digest(legacy)
    row.attachments = [SimpleNamespace(id=uuid4(), content_hash="a" * 64)]
    assert source_digest([row]) != digest(legacy)


def test_send_history_retry_and_no_extra_attachment_charge(client, objects, monkeypatch):
    http, db, captured = client
    selected = db.get(SiteModelProvider, UUID(model_id(http)))
    selected.supports_images = True
    db.commit()
    calls = []
    fail = False
    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            calls.append(messages)
            if fail:
                from app.llm import LLMProviderError
                raise LLMProviderError("test interruption")
            return ToolResponse(content="附件中的内容", tool_calls=[])
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_: Provider())
    image = upload(http, "image.png", PNG)
    assert image.status_code == 201, image.text
    document = upload(http).json()
    conv = create(http, [])
    before = db.scalars(select(CreditLedgerEntry)).all()
    events, payload = turn(http, conv["id"], None, attachment_ids=[image.json()["id"], document["id"]])
    assert events[-1][1]["status"] == "completed", events[-1]
    assert any(part["type"] == "chat_image" for part in calls[0][-1]["content"])
    assert any("Private attached facts" in part.get("text", "") for part in calls[0][-1]["content"])
    detail = http.get(f"/conversations/{conv['id']}").json()
    assert len(detail["messages"][0]["attachments"]) == 2
    assert detail["messages"][0]["content"] == ""
    assert "object_key" not in str(detail) and "Private attached facts" not in str(detail)
    duplicate = http.post(f"/conversations/{conv['id']}/messages", json=payload)
    assert duplicate.status_code == 200 and len(calls) == 1
    fail = True
    failed, _ = turn(http, conv["id"], "followup")
    assert failed[-1][1]["status"] == "failed"
    fail = False
    retried, _ = turn(http, conv["id"], None, retry_message_id=failed[-1][1]["id"])
    assert retried[-1][1]["status"] == "completed"
    assert any(part["type"] == "chat_image" for part in calls[-1][1]["content"])
    # Incompatible history is rejected before a hold or a new message.
    selected.supports_images = False; db.commit()
    count = len(db.scalars(select(CreditLedgerEntry)).all())
    result = http.post(f"/conversations/{conv['id']}/messages", json={
        "request_id": str(uuid4()), "provider_id": str(selected.id), "query": "next"})
    assert result.status_code == 422 and len(calls) == 3
    assert len(db.scalars(select(CreditLedgerEntry)).all()) == count
    assert http.delete(f"/conversations/{conv['id']}").status_code == 204
    assert http.get(f"/chat/attachments/{document['id']}/content").status_code == 200
    attachments.cleanup(db, SimpleNamespace(delete=lambda key: objects.pop(key)))
    assert objects


def test_text_model_accepts_documents_rejects_image_before_hold(client, objects, monkeypatch):
    http, db, _ = client
    conv = create(http, [])
    image = upload(http, "image.png", PNG).json()
    payload = {"request_id": str(uuid4()), "provider_id": model_id(http), "attachment_ids": [image["id"]]}
    count = len(db.scalars(select(CreditLedgerEntry)).all())
    assert http.post(f"/conversations/{conv['id']}/messages", json=payload).status_code == 422
    assert len(db.scalars(select(CreditLedgerEntry)).all()) == count
    document = upload(http).json()
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_: SimpleNamespace(
        complete_with_tools=lambda *args, **kwargs: ToolResponse(content="Read", tool_calls=[])))
    events, _ = turn(http, conv["id"], None, attachment_ids=[document["id"]])
    assert events[-1][1]["status"] == "completed"
    row = db.get(ChatAttachment, UUID(document["id"]))
    assert isinstance(attachments.user_content("", [row]), str)


def test_admin_image_capability_roundtrip(accounts, monkeypatch):
    from test_model_connections import connection, model
    from test_shared_providers import setup_site
    http, db, _ = setup_site(accounts, monkeypatch)
    c = connection(http)
    response = http.post("/site/admin/llm-providers", json={**model(c), "supports_images": True})
    assert response.status_code == 201, response.text
    provider = response.json()
    assert provider["supports_images"]
    assert http.get(f"/site/admin/llm-providers/{provider['id']}").json()["supports_images"]
    public = http.get("/models/available").json()["providers"][0]
    assert public["supports_images"] and "base_url" not in public


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_images_reach_actual_http_stream_and_usage_is_authoritative(objects, protocol):
    from test_conversation_streaming import upstream, sse
    seen = []
    row = SimpleNamespace(id=uuid4(), filename="image.png", kind="image", object_key="private-key",
        content_type="image/png", width=1, height=1, content_hash=hashlib.sha256(PNG).hexdigest())
    objects[row.object_key] = PNG
    def respond(handler, payload):
        seen.append(payload)
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
        if protocol == "responses":
            sse(handler, {"type": "response.output_text.delta", "delta": "看到了图片"})
            sse(handler, {"type": "response.completed", "response": {"status": "completed",
                "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "看到了图片"}]}],
                "usage": {"input_tokens": 235, "output_tokens": 12}}})
        else:
            sse(handler, {"choices": [{"delta": {"content": "看到了图片"}, "finish_reason": "stop"}]})
            sse(handler, {"choices": [], "usage": {"prompt_tokens": 235, "completion_tokens": 12}})
    with upstream(respond) as url:
        provider = OpenAICompatibleChatProvider(config=ChatProviderConfig(
            url, "test-only", "vision", api_protocol=protocol, supports_images=True))
        events = list(provider.stream_with_tools([{"role": "user", "content": attachments.user_content("", [row])}], []))
    assert events[0] == ("delta", {"text": "看到了图片"})
    assert events[-1][1].usage.prompt_tokens == 235 and events[-1][1].usage.completion_tokens == 12
    assert "data:image/png;base64," in repr(seen) and "private-key" not in repr(seen)


def test_compaction_treats_images_as_images_and_retains_original(client, objects, monkeypatch):
    from test_conversation_streaming import context_fixture, summary_provider, compact
    from app.models import ChatMessage, ContextCheckpoint
    http, db, conversation, provider = context_fixture(client, length=60)
    db.get(SiteModelProvider, UUID(provider)).supports_images = True
    item = upload(http, "image.png", PNG).json()
    original = db.scalar(select(ChatMessage).where(
        ChatMessage.conversation_id == UUID(conversation), ChatMessage.sequence == 0))
    original.attachments.append(db.get(ChatAttachment, UUID(item["id"])))
    db.commit(); db.expire_all()
    calls = summary_provider(monkeypatch)
    assert compact(http, conversation, provider)[-1][1]["status"] == "completed"
    summary = calls[0][1]["content"]
    assert summary[1]["type"] == "chat_image"
    assert "_chat/" not in summary[0]["text"]
    assert "record-3" in summary[0]["text"] and "record-4" not in summary[0]["text"]
    db.expire_all()
    assert db.get(ChatMessage, original.id).attachments[0].id == UUID(item["id"])
    assert db.scalars(select(ContextCheckpoint)).first().through_sequence == 3
