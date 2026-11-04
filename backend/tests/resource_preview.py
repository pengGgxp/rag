"""Disposable UI fixture. Storage and upstream model are synthetic, APIs are real."""
import io
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

import chat_attachment_preview as fixture
from app import resource_cache, storage_downloads, workspace_files
from app.models import LoginSession, Membership, User
from test_chat_attachments import PNG


if __name__ == "__main__":
    fixture.Base.metadata.create_all(fixture.engine)
    objects = {}
    def put(source, key, *_):
        objects[key] = source.read()
    def get(key, start, end):
        return io.BytesIO(objects[key][start:end + 1])
    fixture.chat_attachments.upload_fileobj = put
    workspace_files.upload_fileobj = put
    resource_cache.download_range_fileobj = get
    storage_downloads.download_range_fileobj = get
    fixture.chat_attachments.download_fileobj = lambda key: io.BytesIO(objects[key])
    fixture.conversations.OpenAICompatibleChatProvider = lambda **_: fixture.Provider()
    @asynccontextmanager
    async def lifespan(_):
        yield
    fixture.main.app.router.lifespan_context = lifespan
    with fixture.SessionLocal() as db:
        http = fixture.TestClient(fixture.main.app)
        user = fixture.authenticate(http, db)
        user.is_site_admin = True
        user.email_verified_at = fixture.auth.now()
        fixture.configured_model(db, fixture.auth.LEGACY_WORKSPACE_ID, fixture.FixturePatch())
        for model in db.query(fixture.SiteModelProvider):
            model.supports_images = True
            model.context_capacity = 128000
            model.max_output_tokens = 4096
        db.commit()
        for name, data in [("产品说明与团队协作使用记录.txt", "用于检查资源库的中文内容。".encode()),
                ("示例图片.png", PNG)]:
            result = http.post("/resources", files={"file": (name, data)})
            assert result.status_code == 201, result.text
        assert http.get("/storage/allowances").status_code == 200
        cookie = http.cookies.get(fixture.auth.COOKIE)
        member_cookies = {}
        for role in ("viewer", "editor"):
            member = User(email=f"{role}@example.com", name=role, email_verified_at=fixture.auth.now())
            db.add(member); db.flush()
            db.add(Membership(workspace_id=fixture.auth.LEGACY_WORKSPACE_ID, user_id=member.id, role=role))
            token = secrets.token_urlsafe(48)
            db.add(LoginSession(token_hash=fixture.auth.digest(token), user_id=member.id,
                workspace_id=fixture.auth.LEGACY_WORKSPACE_ID, csrf_token=secrets.token_urlsafe(32),
                expires_at=fixture.auth.now()+timedelta(hours=1)))
            member_cookies[role] = token
        db.commit()
    @fixture.main.app.get("/__fixture/session")
    def session_fixture():
        return {"cookie": cookie, **member_cookies}
    fixture.uvicorn.run(fixture.main.app, host="127.0.0.1", port=8127, log_level="warning")
