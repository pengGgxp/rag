"""Local-only browser fixture: real API, local delayed upstream, disposable database.

Run from backend: python tests/conversation_preview.py. No commercial calls or production data.
"""
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

os.environ["FRONTEND_URL"] = os.environ.get("PREVIEW_FRONTEND_URL", "http://127.0.0.1:3110")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-conversation45-')) / 'preview.db'}"
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = "local-fixture-only-not-used"
from cryptography.fernet import Fernet
os.environ["MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
THINKING = os.environ.get("PREVIEW_THINKING") == "1"

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import llm_providers, main
from app.identity import auth
from app.db import Base, engine, SessionLocal
from app.models import ChatMessage, Conversation, ModelBillingSettings, SiteModelProvider
from app.credits import ensure_account
from auth_helpers import authenticate, configured_model
from test_conversation_streaming import upstream, sse
from fastapi.testclient import TestClient
from sqlalchemy import select
import uvicorn


def respond(handler, payload):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    if payload["messages"][0]["content"].startswith("将提供的历史数据"):
        text = "目标：核对发布\n约束：不修改价格\n决定：使用流式输出\n关键事实：检查两次工具调用\n已执行操作：回查历史\n待办：验证"
        sse(handler, {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})
    else:
        tools = sum(message["role"] == "tool" for message in payload["messages"])
        if THINKING and payload.get("enable_thinking"):
            reasoning = "先核对已有记录，再按顺序验证关键细节，最后整理结论。"
            for start in range(0, len(reasoning), 3):
                sse(handler, {"choices": [{"delta": {"reasoning_content": reasoning[start:start+3]}}]})
                time.sleep(0.12)
        text = ["我先回查本次对话中的相关记录。", "已经找到记录，再核对一次关键细节。",
                "### 核对完成\n\n两次工具调用均已完成，以下是结论。\n\n- 保留原始记录。\n- 工具结果按顺序处理。\n\n"
                + ("" if THINKING else "这是流式正文，用于验证向上阅读时不会被强制滚动。\n\n" * 18)][min(tools, 2)]
        for start in range(0, len(text), 7):
            sse(handler, {"choices": [{"delta": {"content": text[start:start+7]}}]})
            time.sleep(0.08)
        if tools < 2:
            sse(handler, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "same-id",
                "function": {"name": "read_conversation_history", "arguments": '{"start_sequence":0,"end_sequence":1}'}}]},
                "finish_reason": "tool_calls"}]})
        else:
            sse(handler, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    sse(handler, {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    llm_providers._decrypt_api_key = lambda _: "local-only"
    @asynccontextmanager
    async def lifespan(_app):
        yield
    main.app.router.lifespan_context = lifespan
    with upstream(respond) as url, SessionLocal() as db:
        client = TestClient(main.app)
        user = authenticate(client, db)
        user.email_verified_at = auth.now()
        user.is_site_admin = True
        binding = configured_model(db, user.id)
        shared = db.get(SiteModelProvider, binding.site_provider_id)
        shared.base_url, shared.context_capacity, shared.max_output_tokens = url, 16000, 2000
        shared.name = "本地流式测试"
        if THINKING:
            shared.thinking_config = {"kind": "effort", "adapter": "bailian",
                "options": [{"id": "high", "label": "高", "value": "high"},
                    {"id": "low", "label": "低", "value": "low"}], "default_option_id": "high"}
            shared.thinking_version = 1
        db.add(ModelBillingSettings(id=1, default_provider_id=shared.id, enabled=False, shared_pricing_enabled=False))
        ensure_account(db, binding.account_id)
        db.commit()
        cookie = client.cookies.get(auth.COOKIE)
        @main.app.get("/__fixture/session")
        def session_fixture():
            return {"cookie": cookie}
        @main.app.post("/__fixture/history")
        def history_fixture(conversation_id: str | None = None):
            with SessionLocal() as session:
                from uuid import UUID
                conversation = session.get(Conversation, UUID(conversation_id)) if conversation_id else session.scalar(
                    select(Conversation).order_by(Conversation.created_at.desc()))
                if conversation.active_request_id:
                    return {"busy": True}
                for index in range(8):
                    session.add(ChatMessage(conversation_id=conversation.id, sequence=conversation.next_sequence+index,
                        role="user" if index % 2 == 0 else "assistant", status="completed",
                        content=f"早期记录 {index} " + "历史工作内容。" * 60))
                conversation.next_sequence += 8
                session.commit()
                return {"id": str(conversation.id)}
        port = int(os.environ.get("PREVIEW_PORT", "8125"))
        print(f"Local fixture listening at http://127.0.0.1:{port}", flush=True)
        uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")
