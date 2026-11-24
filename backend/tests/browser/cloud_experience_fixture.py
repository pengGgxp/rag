"""Isolated UI acceptance server. Never load production settings or cloud credentials."""
import io
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

if os.environ.get("DATABASE_URL") != "sqlite:////tmp/cloud-107-acceptance.db":
    raise RuntimeError("This fixture requires its disposable database")
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import cloud_computer as cloud, resource_cache, shared_resources, storage_backend, storage_downloads

from app.identity import auth
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.credits import authorization_hold, ensure_account
from app.main import app
from app.models import ChatMessage, CloudComputerCommand, CloudComputerSession, CloudPreview, User
from app.identity.site_auth import protect
from test_cloud_computer import FakeCloud
from test_cloud_computer_mount import MemoryMount

settings = get_settings()
if settings.database_url != "sqlite:////tmp/cloud-107-acceptance.db" or settings.llm_api_key:
    raise RuntimeError("This fixture requires its disposable database and no model credentials")

store = MemoryMount()
storage_backend.active_settings = lambda _db: SimpleNamespace(provider="oss", bucket=store.bucket, region=store.region)
storage_backend.current_storage = lambda _db: store
shared_resources.backend = lambda _db: store
def download(key, start, end):
    return io.BytesIO(store.objects[key][start:end + 1])
resource_cache.download_range_fileobj = download
storage_downloads.download_range_fileobj = download
fake = FakeCloud()
fake.preview_alive = lambda *_: True
fake.stop_preview = lambda *_: None
cloud.adapter = lambda *_: fake

init_db()
client = TestClient(app, headers={"X-RAG-Request": "1"})
email, password = "cloud107-local@example.com", "cloud107-local-only-123"
identity = client.post("/auth/register", json={"email": email, "name": "云电脑体验验收", "password": password})
assert identity.status_code == 201, identity.text
identity = identity.json()
client.headers["X-CSRF-Token"] = identity["csrf_token"]
with SessionLocal() as db:
    db.get(User, UUID(identity["user"]["id"])).is_site_admin = True
    account = ensure_account(db, UUID(identity["user"]["id"]))
    account.balance = Decimal("100")
    account.lifetime_granted = Decimal("100")
    db.commit()
configuration = {"version": 0, "enabled": True, "region": "cn-hangzhou", "template": "mock-only",
    "specification": "isolated-local", "api_key": "synthetic-no-cloud-access",
    "credits_per_minute": "2.1234", "oss_role_arn": "acs:ram::123:role/local-test"}
saved = client.put("/site/admin/cloud-computer", json=configuration)
assert saved.status_code == 200, saved.text
html = Path(__file__).with_name("cloud-preview.html").read_bytes()
uploaded = client.post("/resources", files={"file": ("排序可视化.html", html, "text/html")})
assert uploaded.status_code == 201, uploaded.text
file = uploaded.json()
conversation = client.post("/conversations", json={})
assert conversation.status_code == 201, conversation.text
conversation_id = UUID(conversation.json()["id"])
with SessionLocal() as db:
    started = auth.now() - timedelta(seconds=149)
    from app.models import CloudComputerSettings, Conversation
    config = db.get(CloudComputerSettings, 1)
    computer = CloudComputerSession(account_id=UUID(identity["user"]["id"]), user_id=UUID(identity["user"]["id"]),
        conversation_id=conversation_id, session_hash=auth.digest(client.cookies.get(auth.COOKIE)), status="ready",
        provider_id="local-synthetic", snapshot={**config.config, "version": config.version}, encrypted_api_key=protect("fake"),
        held_credits=Decimal("20"), started_at=started, last_activity_at=auth.now())
    db.add(computer); db.flush()
    authorization_hold(db, computer.account_id, computer.user_id, uuid5(computer.id, "hold:0"), Decimal("20"))
    fake.instances["local-synthetic"] = {"files": {}, "started_at": started}
    command = CloudComputerCommand(session_id=computer.id, request_id=uuid4(), digest="a" * 64, payload={},
        status="completed", started_at=started, finished_at=auth.now(), result={"exit_code": 0})
    db.add(command); db.flush()
    preview = CloudPreview(session_id=computer.id, command_id=command.id, port=8080, status="ready",
        name="冒泡排序可视化 · 可交互、可重置的算法演示", upstream_host="mock-not-routed",
        encrypted_traffic_token=protect("fake"))
    db.add(preview); db.flush()
    preview_read = {"id": str(preview.id), "name": preview.name, "status": "ready",
        "terminal": False, "summary": "预览已就绪", "url": None}
    tools = [
        {"call_id": "write", "name": "write_file", "label": "生成文件", "kind": "tool", "status": "completed",
         "outcome": "success", "files": [file], "summary": "文件已生成"},
        {"call_id": "preview", "name": "start_preview", "label": "启动成果预览", "kind": "tool",
         "status": "completed", "outcome": "success", "preview": preview_read},
    ]
    blocks = [{"id": "write", "type": "activity", "call_id": "write"},
        {"id": "preview", "type": "activity", "call_id": "preview"},
        {"id": "answer", "type": "text", "text": "已完成冒泡排序可视化页面，包含单步比较、交换和重置。可以打开预览直接操作，也可以查看文件。"}]
    db.add_all([
        ChatMessage(conversation_id=conversation_id, sequence=0, role="user", content="做一个可以单步操作的冒泡排序页面。", status="completed"),
        ChatMessage(conversation_id=conversation_id, sequence=1, role="assistant", content=blocks[-1]["text"],
            status="completed", tool_events=tools, display_blocks=blocks),
    ])
    db.get(Conversation, conversation_id).title = "冒泡排序可视化"
    db.commit()
    receipt = {"email": email, "password": password, "conversation_id": str(conversation_id),
        "workspace_id": identity["user"]["id"], "preview_id": str(preview.id), "file_id": file["id"]}
Path("/tmp/cloud-107-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
