"""Disposable real-API browser fixture: local files, Workers and model; no paid IO."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet

root = Path(tempfile.mkdtemp(prefix="rag-issue91-"))
os.environ["FRONTEND_URL"] = "http://localhost:3191"
os.environ["API_PUBLIC_URL"] = "http://localhost:8131"
os.environ["DATABASE_URL"] = f"sqlite:///{root / 'preview.db'}"
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import auth, cloud_computer as cloud, conversation_jobs, conversations, llm_providers, main
from app import shared_resources, storage_backend, storage_downloads, workspace_files
from app.db import Base, engine, SessionLocal
from app.llm import ToolCall, ToolResponse
from app.models import CloudComputerSettings, LEGACY_WORKSPACE_ID, ModelBillingSettings, SiteStorageSettings
from app.site_auth import protect
from auth_helpers import authenticate, configured_model
from cloud_command_fixtures import LocalFileCloud
from test_cloud_computer_mount import MemoryMount
from types import SimpleNamespace
import uvicorn

observations = []
path = "验收/数量.txt"


class Provider:
    def complete_with_tools(self, messages, tools, *, tool_choice):
        results = [json.loads(item["content"]) for item in messages
            if item.get("role") == "tool" and item.get("name") in ("read_file", "apply_patch")]
        # Run once per fixture process, which seeds an isolated original file.
        step = len(results)
        if step:
            observations.append(results[-1])
        if step == 3:
            assert results[0]["content"] == "quantity 17\n"
            assert results[2]["content"] == "quantity 23\nchecked\n"
            return ToolResponse("修改前数量为 17，修改后重新读取数量为 23，新增 checked 行已保存。", [])
        name, arguments = ("apply_patch", {"path": path,
            "patch": "@@ -1 +1,2 @@\n-quantity 17\n+quantity 23\n+checked\n"}) if step == 1 else (
                "read_file", {"path": path})
        return ToolResponse(None, [ToolCall("same-provider-call-id", name, arguments)])


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    storage = MemoryMount()
    shared_resources.backend = lambda _: storage
    storage_backend.current_storage = lambda _, **kwargs: storage
    storage_backend.active_settings = lambda _: SimpleNamespace(provider="oss", bucket=storage.bucket, region=storage.region)
    workspace_files.upload_fileobj = lambda stream, key, *_: storage.objects.__setitem__(key, stream.read())
    storage_downloads.download_range_fileobj = lambda key, start, end: io.BytesIO(storage.objects[key][start:end+1])
    llm_providers._decrypt_api_key = lambda _: "local-only"
    conversations.OpenAICompatibleChatProvider = lambda **_: Provider()
    conversation_jobs.dispatch = lambda identifier: Thread(target=conversation_jobs.run,
        args=(identifier,), kwargs={"factory": SessionLocal, "retrieve": main.retrieve_knowledge_base}, daemon=True).start()
    from app import tasks
    tasks.name_conversation.delay = lambda *_: None
    @asynccontextmanager
    async def lifespan(_app):
        yield
    main.app.router.lifespan_context = lifespan
    with SessionLocal() as db:
        client = TestClient(main.app)
        user = authenticate(client, db)
        user.email_verified_at = auth.now()
        configured_model(db, LEGACY_WORKSPACE_ID)
        db.add(ModelBillingSettings(id=1, enabled=False, shared_pricing_enabled=False))
        db.add(SiteStorageSettings(id=2, version=1, provider="oss", bucket=storage.bucket, region=storage.region,
            encrypted_secret_id="local-only", encrypted_secret_key="local-only", active=True))
        db.add(CloudComputerSettings(id=1, version=1, enabled=True,
            config=cloud.Configuration(region=storage.region, template="local-only", specification="local-only",
                credits_per_minute=0, oss_role_arn="acs:ram::123:role/test").model_dump(mode="json"),
            encrypted_api_key=protect("local-only")))
        db.commit()
        session = db.get(auth.LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
        access = auth.Access(user.id, LEGACY_WORKSPACE_ID, "admin", session.token_hash)
        resource = workspace_files._save_bytes(access.workspace_id, access, db, path, b"quantity 17\n", "text/plain")
        cookie = client.cookies.get(auth.COOKIE)
    provider = LocalFileCloud(root / "workspace", storage, shared_resources.prefix(LEGACY_WORKSPACE_ID))
    cloud.adapter = lambda *_: provider
    cloud.dispatch = lambda identifier: Thread(target=cloud.run,
        args=(identifier,), kwargs={"factory": SessionLocal}, daemon=True).start()

    @main.app.get("/__fixture/session")
    def fixture_session():
        return {"cookie": cookie, "resource": resource, "temporary_directory": str(root)}

    @main.app.get("/__fixture/report")
    def fixture_report():
        with SessionLocal() as db:
            commands = db.scalars(select(cloud.Command).order_by(cloud.Command.created_at)).all()
            content = storage.objects[shared_resources.prefix(LEGACY_WORKSPACE_ID) + path]
            return {"creates": provider.creates, "starts": provider.starts,
                "commands": [{"id": str(row.id), "request_id": str(row.request_id), "digest": row.digest,
                    "status": row.status, "operation": row.payload.get("file_operation", {}).get("name")}
                    for row in commands], "observations": observations,
                "content": content.decode(), "sha256": hashlib.sha256(content).hexdigest()}

    print(f"本地隔离验收目录：{root}", flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=8131, log_level="warning")
