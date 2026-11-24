"""Disposable browser fixture: actual attachment/conversation APIs, local object storage and model."""
import io
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import asynccontextmanager

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3110"
os.environ["DATABASE_URL"] = os.environ.get("PREVIEW_DATABASE_URL") or f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-chat56-')) / 'preview.db'}"
from cryptography.fernet import Fernet
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import uvicorn
from app import main
from app.identity import auth
from app.conversations import attachments as chat_attachments, execution as conversations
from app.infra.db import Base, SessionLocal, engine
from app.model_runtime.client import LLMUsage, ToolResponse
from app.models import KnowledgeBase, SiteModelProvider
from auth_helpers import authenticate, configured_model


class FixturePatch:
    def setattr(self, target, name, value=None):
        if value is None:
            import importlib
            module, attr = target.rsplit(".", 1)
            setattr(importlib.import_module(module), attr, name)
        else:
            setattr(target, name, value)


class Provider:
    last_usage = None
    stream_diagnostic = "sse"
    def complete_with_tools(self, *args, **kwargs):
        raise AssertionError("Expected streaming")
    def stream_with_tools(self, messages, tools, **kwargs):
        content = messages[-1]["content"]
        parts = content if isinstance(content, list) else []
        text = ("已收到图片，可以帮你分析图中的内容。" if any(part["type"] == "chat_image" for part in parts)
                else "已阅读附件，可以结合其中的内容回答你的问题。")
        for fragment in [text[:8], text[8:16], text[16:]]:
            time.sleep(.18)
            yield "delta", {"text": fragment}
        self.last_usage = LLMUsage(100, 20, 120)
        yield "complete", ToolResponse(content=text, tool_calls=[], usage=self.last_usage)


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    objects = {}
    chat_attachments.upload_fileobj = lambda source, key, mime: objects.update({key: source.read()})
    chat_attachments.download_fileobj = lambda key: io.BytesIO(objects[key])
    conversations.OpenAICompatibleChatProvider = lambda **kwargs: Provider()
    @asynccontextmanager
    async def lifespan(_):
        yield
    main.app.router.lifespan_context = lifespan
    with SessionLocal() as db:
        http = TestClient(main.app)
        user = authenticate(http, db)
        user.is_site_admin = True
        user.email_verified_at = auth.now()
        selected = configured_model(db, user.id, FixturePatch())
        for model in db.query(SiteModelProvider).all():
            model.supports_images = True
            model.name = "视觉模型"
            model.context_capacity = 128000
            model.max_output_tokens = 4096
            model.thinking_config = {"kind": "effort", "adapter": "openai",
                "options": [{"id": "low", "label": "快速", "value": "low"},
                    {"id": "high", "label": "深入思考", "value": "high"}], "default_option_id": "high"}
            model.thinking_version = 1
            db.add(SiteModelProvider(key="text-fixture", name="文字模型", model="text-only",
                connection_id=model.connection_id, enabled=True, prices=model.prices,
                context_capacity=128000, max_output_tokens=4096))
        db.commit()
        cookie = http.cookies.get(auth.COOKIE)
    @main.app.get("/__fixture/session")
    def session_fixture():
        return {"cookie": cookie}
    print("Attachment browser fixture: http://127.0.0.1:8127 (local storage and model only)", flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=8127, log_level="warning")
