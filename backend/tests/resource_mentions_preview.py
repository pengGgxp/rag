"""Disposable real-API browser fixture; no paid upstream or cloud operations."""
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet

os.environ["FRONTEND_URL"] = "http://localhost:3110"
os.environ["API_PUBLIC_URL"] = "http://localhost:8126"
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-resource-mentions-')) / 'preview.db'}"
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import auth, conversation_jobs, conversations, llm_providers, main, shared_resources, workspace_files
from app.db import Base, engine, SessionLocal
from app.llm import ToolResponse
from app.models import LEGACY_WORKSPACE_ID, ModelBillingSettings
from auth_helpers import authenticate, configured_model
from test_cloud_computer_mount import MemoryMount
from test_retrieval import add_document, add_library
import uvicorn


class Provider:
    def complete_with_tools(self, messages, tools, *, tool_choice):
        return ToolResponse(content="已收到文件引用，本轮没有执行文件操作。", tool_calls=[])


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    storage = MemoryMount()
    shared_resources.backend = lambda _: storage
    workspace_files.upload_fileobj = lambda stream, key, *_: storage.objects.__setitem__(key, stream.read())
    llm_providers._decrypt_api_key = lambda _: "local-only"
    conversations.OpenAICompatibleChatProvider = lambda **_: Provider()
    conversation_jobs.dispatch = lambda identifier: Thread(target=conversation_jobs.run,
        args=(identifier,), kwargs={"factory": SessionLocal, "retrieve": main.retrieve_knowledge_base}, daemon=True).start()

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
        library = add_library(db)
        library.name = "产品资料"
        document = add_document(db, library, "synthetic evidence")
        document.filename = "项目说明.txt"
        db.commit()
        session = db.get(auth.LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
        access = auth.Access(user.id, LEGACY_WORKSPACE_ID, "admin", session.token_hash)
        for path in ["项目说明.txt", "项目/交付/项目说明.txt", "研发/需求/交付验收与资源文件路径展示长名称检查.txt"]:
            workspace_files._save_bytes(access.workspace_id, access, db, path, b"LOCAL SYNTHETIC FILE", "text/plain")
        cookie = client.cookies.get(auth.COOKIE)

    @main.app.get("/__fixture/session")
    def fixture_session():
        return {"cookie": cookie}

    @main.app.post("/__fixture/remove")
    def fixture_remove():
        storage.objects.clear()
        return {"removed": True}

    print("本地隔离验收 API：http://localhost:8126", flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=8126, log_level="warning")
