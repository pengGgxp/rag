"""Local browser acceptance fixture; isolated DB, real Worker, local-only model HTTP."""
import os
from pathlib import Path
import sys
import tempfile

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3119"
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-retry89-')) / 'preview.db'}"
from cryptography.fernet import Fernet
os.environ["MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = os.environ["MCP_CREDENTIAL_KEY"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collections import Counter
from contextlib import asynccontextmanager
from threading import Thread
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
import uvicorn

from app import auth, llm_providers, main
from app.conversations import jobs as conversation_jobs
from app.db import Base, engine, SessionLocal
from app.models import (ChatMessage, Conversation, ConversationExecution, ModelBillingSettings,
    ModelConnection, SiteModelProvider)
from app.credits import ensure_account
from auth_helpers import authenticate, configured_model
from test_conversation_streaming import upstream, sse

attempts = Counter()


def respond(handler, payload):
    text = next((item.get("content", "") for item in reversed(payload["messages"]) if item["role"] == "user"), "")
    key = str(text)
    attempts[key] += 1
    status = 503 if "耗尽" in key else 429 if ("停止" in key or attempts[key] == 1) else 200
    handler.send_response(status)
    if status != 200:
        handler.send_header("Retry-After", "6")
        handler.end_headers()
        handler.wfile.write(b'{"error":"local-only simulated upstream failure"}')
        return
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    sse(handler, {"choices": [{"delta": {"content": "服务已经恢复，本次回答已完成。"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    llm_providers._decrypt_api_key = lambda _: "local-fixture-only"
    @asynccontextmanager
    async def lifespan(_app):
        yield
    main.app.router.lifespan_context = lifespan
    conversation_jobs.queue_title = lambda *args: None
    def dispatch(identifier):
        Thread(target=conversation_jobs.run, args=(identifier,), daemon=True).start()
    conversation_jobs.dispatch = dispatch
    with upstream(respond) as url, SessionLocal() as db:
        client = TestClient(main.app)
        user = authenticate(client, db)
        user.email_verified_at = auth.now()
        binding = configured_model(db, user.id)
        shared = db.get(SiteModelProvider, binding.site_provider_id)
        db.get(ModelConnection, shared.connection_id).base_url = url
        shared.context_capacity, shared.max_output_tokens = 16000, 2000
        shared.name = "本地重试验收"
        db.add(ModelBillingSettings(id=1, default_provider_id=shared.id, enabled=False, shared_pricing_enabled=False))
        ensure_account(db, binding.account_id)
        conversation = Conversation(account_id=user.id, owner_id=user.id, title="上游重试验收",
            title_source="manual")
        db.add(conversation)
        db.commit()
        cookie, identifier = client.cookies.get(auth.COOKIE), str(conversation.id)
        @main.app.get("/__fixture/login")
        def login():
            response = RedirectResponse(f"http://127.0.0.1:3119/?conversation={identifier}")
            response.set_cookie(auth.COOKIE, cookie, httponly=True, samesite="lax")
            return response
        @main.app.get("/__fixture/status")
        def status():
            with SessionLocal() as session:
                messages = list(session.scalars(select(ChatMessage).order_by(ChatMessage.sequence)))
                executions = list(session.scalars(select(ConversationExecution)))
                return {"attempts": dict(attempts), "messages": [
                    {"role": message.role, "status": message.status, "activities": message.tool_events}
                    for message in messages], "executions": len(executions)}
        print("本地重试验收服务：http://127.0.0.1:8129/__fixture/login", flush=True)
        uvicorn.run(main.app, host="127.0.0.1", port=8129, log_level="warning")
