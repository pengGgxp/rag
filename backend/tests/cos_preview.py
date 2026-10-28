"""Disposable browser fixture: real upload API and worker admission, local COS-shaped HTTP server."""
import hashlib
import os
import secrets
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3110"
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-cos46-')) / 'preview.db'}"
from cryptography.fernet import Fernet
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, document_uploads, main, storage_settings, tasks
from app.db import Base, SessionLocal, engine
from app.models import Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase, Membership, User, Workspace
from auth_helpers import authenticate
from fastapi.testclient import TestClient
from test_cos_uploads import FakeCos
import uvicorn

storage = FakeCos()
tokens = {}


def part_url(key, identifier, number, length):
    token = secrets.token_urlsafe(24)
    tokens[token] = (identifier, number, length)
    return f"http://127.0.0.1:8136/{token}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3110")
        self.send_header("Access-Control-Allow-Methods", "PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Expose-Headers", "ETag")

    def do_OPTIONS(self):
        self.send_response(200); self.cors(); self.end_headers()

    def do_PUT(self):
        target = tokens.get(self.path[1:])
        if not target or int(self.headers.get("Content-Length", 0)) != target[2]:
            self.send_response(403); self.cors(); self.end_headers(); return
        identifier, number, length = target
        blocks = []
        while length > 0:
            block = self.rfile.read(min(length, 128 * 1024))
            if not block:
                return
            blocks.append(block); length -= len(block)
            time.sleep(0.06)
        if identifier not in storage.uploads:
            self.send_response(404); self.cors(); self.end_headers(); return
        storage.uploads[identifier]["parts"][number] = b"".join(blocks)
        self.send_response(200); self.cors(); self.send_header("ETag", '"local-part"'); self.end_headers()


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    server = ThreadingHTTPServer(("127.0.0.1", 8136), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    storage.part_url = part_url
    document_uploads.current_cos = lambda *_: storage
    storage_settings.CosStorage = lambda *_: storage
    @asynccontextmanager
    async def lifespan(_):
        yield
    main.app.router.lifespan_context = lifespan
    def verify(identifier):
        def work():
            time.sleep(2)
            document_uploads.finalize_upload(identifier)
        threading.Thread(target=work, daemon=True).start()
    tasks.verify_upload.delay = verify
    def local_index(document_id, job_id):
        with SessionLocal() as db:
            document = db.get(Document, UUID(document_id))
            document.status = DocumentStatus.READY
            db.get(IngestionJob, UUID(job_id)).state = JobState.SUCCEEDED
            db.commit()
    tasks.embed_document.delay = local_index
    with SessionLocal() as db:
        client = TestClient(main.app)
        user = authenticate(client, db)
        user.email_verified_at, user.is_site_admin = auth.now(), True
        library = KnowledgeBase(workspace_id=auth.LEGACY_WORKSPACE_ID, name="COS 上传验收")
        other = KnowledgeBase(workspace_id=auth.LEGACY_WORKSPACE_ID, name="另一知识库")
        alternate = Workspace(name="另一测试工作区")
        db.add_all([library, other, alternate]); db.flush()
        db.add(Membership(workspace_id=alternate.id, user_id=user.id, role="admin"))
        db.commit()
        cookie = client.cookies.get(auth.COOKIE)
        ids = {"library": str(library.id), "other": str(other.id), "workspace": str(auth.LEGACY_WORKSPACE_ID),
            "alternate": str(alternate.id)}
        @main.app.get("/__fixture/session")
        def session_fixture():
            return {"cookie": cookie, **ids}
    print("COS browser fixture: http://127.0.0.1:8126 (not Tencent Cloud)", flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=8126, log_level="warning")
