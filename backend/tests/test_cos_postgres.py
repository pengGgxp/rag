import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, document_uploads, main, tasks
from app.models import Document, DocumentUpload, KnowledgeBase, LoginSession, Membership, User, Workspace
from test_auth_postgres import auth_postgres
from test_cos_uploads import FakeCos


def test_parallel_sealing_and_verifying_admits_only_once(auth_postgres, monkeypatch):
    engine = auth_postgres
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    storage = FakeCos()
    monkeypatch.setattr(document_uploads, "current_storage", lambda _: storage)
    monkeypatch.setattr(document_uploads, "SessionLocal", lambda: Session(engine, expire_on_commit=False))
    monkeypatch.setattr(tasks.verify_upload, "delay", lambda _: None)
    processed = []
    monkeypatch.setattr(tasks.process_document, "run", lambda *a, **kw: processed.append(a))
    with Session(engine) as db:
        workspace = Workspace(name="COS isolation")
        user = User(name="Uploader", email=f"cos-{uuid4().hex}@example.com", email_verified_at=auth.now())
        db.add_all([workspace, user]); db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id, workspace_id=workspace.id,
            expires_at=auth.now()+timedelta(hours=1)))
        kb = KnowledgeBase(workspace_id=workspace.id, name="Multipart tests")
        db.add(kb); db.commit(); kb_id = kb.id
    headers = {"X-RAG-Request": "1", "X-CSRF-Token": csrf}
    with TestClient(main.app, headers=headers) as client:
        client.cookies.set(auth.COOKIE, raw)
        result = client.post(f"/knowledge-bases/{kb_id}/uploads", json={"filename": "sample.txt", "size_bytes": 6})
        assert result.status_code == 201, result.text
        identifier = result.json()["id"]
        assert client.post(f"/uploads/{identifier}/parts/1", json={"sha256": hashlib.sha256(b"sample").hexdigest()}).status_code == 200
    with Session(engine) as db:
        row = db.get(DocumentUpload, UUID(identifier))
        storage.uploads[row.multipart_id]["parts"][1] = b"sample"
    barrier = Barrier(3)
    def seal(_):
        with TestClient(main.app, headers=headers) as client:
            client.cookies.set(auth.COOKIE, raw)
            barrier.wait(timeout=10)
            response = client.post(f"/uploads/{identifier}/complete")
            assert response.status_code == 202, response.text
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(seal, range(3)))
    assert storage.seals == 1
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: document_uploads.finalize_upload(identifier), range(3)))
    assert len(processed) == storage.downloads == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Document).where(Document.knowledge_base_id == kb_id)) == 1
