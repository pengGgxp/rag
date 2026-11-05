"""Disposable image UI acceptance server; no external generation or storage."""
import io
import os
import secrets
from contextlib import asynccontextmanager
from threading import Lock, Thread

import chat_attachment_preview as fixture
from app import image_adapters, image_tasks, storage_downloads, workspace_files
from app.config import get_settings
from app.llm_providers import _encrypt_api_key
from app.models import ImageModelSettings, ModelConnection
from PIL import Image
from app.llm import LLMUsage, ToolCall, ToolResponse

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3113"
get_settings.cache_clear()

if __name__ == "__main__":
    for middleware in fixture.main.app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            middleware.kwargs["allow_origins"].append("http://127.0.0.1:3113")
    fixture.Base.metadata.create_all(fixture.engine)
    objects = {}
    delivery_failure = [False]
    def put(stream, key, *_):
        if delivery_failure[0]:
            delivery_failure[0] = False
            raise OSError("synthetic storage failure")
        objects[key] = stream.read()
    workspace_files.upload_fileobj = put
    storage_downloads.download_range_fileobj = lambda key, start, end: io.BytesIO(objects[key][start:end + 1])
    class ImageProvider(fixture.Provider):
        def stream_with_tools(self, messages, tools, **kwargs):
            usage = LLMUsage(100, 20, 120)
            if messages[-1]["role"] != "tool":
                yield "complete", ToolResponse(content=None, tool_calls=[ToolCall("image-ui", "generate_image", {"prompt": "合成蓝色方块"})], usage=usage)
            else:
                yield "delta", {"text": "图片任务已提交。"}
                yield "complete", ToolResponse(content="图片任务已提交。", tool_calls=[], usage=usage)
            self.last_usage = usage
    fixture.conversations.OpenAICompatibleChatProvider = lambda **_: ImageProvider()
    @asynccontextmanager
    async def lifespan(_):
        yield
    fixture.main.app.router.lifespan_context = lifespan
    def generate(snapshot, secret, payload, references):
        if payload["prompt"] == "synthetic-uncertain":
            raise image_adapters.ImageUncertain("synthetic response lost", "synthetic-uncertain-request")
        if payload["prompt"] == "synthetic-save-failure":
            delivery_failure[0] = True
        stream = io.BytesIO()
        Image.new("RGB", (640, 480), "#2878b5").save(stream, "PNG")
        return [(stream.getvalue(), "image/png")], "synthetic-ui", {"images": 1}
    image_adapters.generate = generate
    submitted, dispatch_lock = set(), Lock()
    def dispatch(identifier):
        with dispatch_lock:
            if str(identifier) in submitted:
                return
            submitted.add(str(identifier))
        def run():
            try:
                image_tasks.run(identifier)
            finally:
                with dispatch_lock:
                    submitted.discard(str(identifier))
        Thread(target=run, daemon=True).start()
    image_tasks.dispatch = dispatch
    with fixture.SessionLocal() as db:
        http = fixture.TestClient(fixture.main.app)
        user = fixture.authenticate(http, db)
        user.is_site_admin = True
        user.email_verified_at = fixture.auth.now()
        fixture.configured_model(db, fixture.auth.LEGACY_WORKSPACE_ID)
        for connection in db.query(ModelConnection):
            connection.encrypted_api_key = _encrypt_api_key("synthetic-only")
        connection = ModelConnection(name="图片验收供应商", base_url="https://synthetic.invalid/v1", encrypted_api_key=_encrypt_api_key("synthetic-only"), website_url="https://example.com")
        db.add(connection); db.flush()
        model = fixture.SiteModelProvider(key=secrets.token_hex(10), name="图片验收模型", model="synthetic-image", connection_id=connection.id, enabled=True,
            supports_chat=False, image_config={"protocol": "openai_images", "generate": True, "edit": True, "max_count": 1, "max_references": 1,
            "default_size": "1024x1024", "default_quality": "standard", "specs": [{"operation": operation, "size": "1024x1024", "quality": "standard", "credits_per_image": .25, "cost_per_image": None} for operation in ("generate", "edit")]})
        db.add(model); db.flush()
        db.add(ImageModelSettings(id=1, generate_model_id=model.id, edit_model_id=model.id, version=1)); db.commit()
        cookie = http.cookies.get(fixture.auth.COOKIE)
        model_id = str(model.id)
    @fixture.main.app.get("/__fixture/session")
    def session_fixture():
        return {"cookie": cookie, "model_id": model_id}
    fixture.uvicorn.run(fixture.main.app, host="127.0.0.1", port=8128, log_level="warning")
