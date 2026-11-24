import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import main, tasks
from app.files import quotas as storage_billing

from app.identity import auth
from app.knowledge.ingestion import uploads as document_uploads
from app.knowledge.ingestion.recovery import recover_interrupted_ingestion, worker_claim
from app.models import Document, DocumentStatus, DocumentUpload, IngestionJob, JobState, KnowledgeBase, LoginSession, User
from test_auth_postgres import auth_postgres
from test_cos_uploads import FakeCos


@pytest.mark.parametrize("revoke_during_download", [False, True])
def test_verifying_upload_does_not_lock_account_during_download(auth_postgres, monkeypatch, revoke_during_download):
    engine = auth_postgres
    storage = FakeCos()
    monkeypatch.setattr(document_uploads, "current_storage", lambda _: storage)
    monkeypatch.setattr("app.files.operations.storage", lambda _: storage)
    monkeypatch.setattr(document_uploads, "SessionLocal", lambda: Session(engine, expire_on_commit=False))
    processed = []
    monkeypatch.setattr(tasks.process_document, "delay", processed.append)
    monkeypatch.setattr(tasks.verify_upload, "apply_async", lambda **_: None)
    identifier = uuid4()
    with Session(engine) as db:
        user = User(name="Uploader", email=f"cos-{uuid4().hex}@example.com", email_verified_at=auth.now())
        db.add(user)
        db.flush()
        kb = KnowledgeBase(account_id=user.id, owner_id=user.id, name="Concurrent upload")
        db.add(kb)
        db.flush()
        access = auth.Access(user.id, "")
        kb_id = kb.id
        key = f"{kb_id}/{identifier}.txt"
        storage.objects[key] = b"sample"
        db.add(DocumentUpload(id=identifier, user_id=user.id, account_id=user.id,
            knowledge_base_id=kb_id, filename="first.txt", content_type="text/plain",
            size_bytes=6, object_key=key, multipart_id="sealed", request_key=uuid4(),
            parts={"1": hashlib.sha256(b"sample").hexdigest()}, state="verifying",
            expires_at=auth.now()+timedelta(hours=1)))
        db.commit()

    downloading, release = Event(), Event()
    download = storage.download

    def blocked_download(key):
        downloading.set()
        assert release.wait(timeout=10)
        return download(key)

    monkeypatch.setattr(storage, "download", blocked_download)

    def concurrent_change():
        with Session(engine) as db:
            if revoke_during_download:
                db.get(User, access.user_id).is_suspended = True
                db.commit()
                return
            result = document_uploads.create_upload(kb_id, document_uploads.UploadInput(
                filename="next.txt", size_bytes=6), idempotency_key=uuid4(), db=db, access=access)
            assert result["state"] == "uploading"

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(document_uploads.finalize_upload, str(identifier))
        try:
            assert downloading.wait(timeout=5)
            pool.submit(concurrent_change).result(timeout=3)
        finally:
            release.set()
        worker.result(timeout=5)
    with Session(engine) as db:
        row = db.get(DocumentUpload, identifier)
        assert row.state == ("failed" if revoke_during_download else "completed")
        if revoke_during_download:
            assert row.failure["code"] == "permission_denied"
            assert row.document_id is None and not processed
        else:
            assert row.document_id and len(processed) == 1


def test_account_capacity_lock_serializes_writers_without_blocking_foreign_keys(auth_postgres):
    with Session(auth_postgres) as capacity, Session(auth_postgres) as other:
        user = User(name="Capacity lock", email=f"capacity-{uuid4().hex}@example.com")
        capacity.add(user)
        capacity.commit()
        identifier = user.id
        storage_billing.lock_workspace(capacity, identifier)
        row = select(User.id).where(User.id == identifier)
        assert other.scalar(row.with_for_update(read=True, key_share=True, nowait=True)) == identifier
        with pytest.raises(OperationalError) as conflict:
            other.scalar(row.with_for_update(key_share=True, nowait=True))
        assert conflict.value.orig.sqlstate == "55P03"


def test_parallel_sealing_and_verifying_admits_only_once(auth_postgres, monkeypatch):
    engine = auth_postgres
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    storage = FakeCos()
    published = []
    read_keys = []
    download = storage.download

    def copy(source, target, *, etag=None):
        storage.objects[target] = storage.objects[source]
        if "/knowledge-originals/" in target:
            published.append(target)

    def record_download(key):
        read_keys.append(key)
        return download(key)

    monkeypatch.setattr(storage, "copy", copy, raising=False)
    monkeypatch.setattr(storage, "download", record_download)
    monkeypatch.setattr(document_uploads, "current_storage", lambda _: storage)
    monkeypatch.setattr("app.files.operations.storage", lambda _: storage)
    monkeypatch.setattr(document_uploads, "SessionLocal", lambda: Session(engine, expire_on_commit=False))
    monkeypatch.setattr(tasks.verify_upload, "delay", lambda _: None)
    processed = []
    monkeypatch.setattr(tasks.process_document, "delay", lambda *a, **kw: processed.append(a))
    with Session(engine) as db:
        user = User(name="Uploader", email=f"cos-{uuid4().hex}@example.com", email_verified_at=auth.now())
        db.add(user); db.flush()
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id,
            expires_at=auth.now()+timedelta(hours=1)))
        kb = KnowledgeBase(account_id=user.id, owner_id=user.id, name="Multipart tests")
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
        sealed_key = row.object_key
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
    assert len(processed) == read_keys.count(sealed_key) == len(published) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Document).where(Document.knowledge_base_id == kb_id)) == 1
        assert db.scalar(select(func.count()).select_from(IngestionJob).join(
            Document, Document.id == IngestionJob.document_id
        ).where(Document.knowledge_base_id == kb_id)) == 1


def test_parallel_upload_creation_reuses_idempotency_session(auth_postgres, monkeypatch):
    engine = auth_postgres
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    storage = FakeCos()
    monkeypatch.setattr(document_uploads, "current_storage", lambda _: storage)
    monkeypatch.setattr(document_uploads, "SessionLocal", lambda: Session(engine, expire_on_commit=False))
    with Session(engine) as db:
        user = User(name="Uploader", email=f"cos-{uuid4().hex}@example.com", email_verified_at=auth.now())
        db.add(user); db.flush()
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id,
            expires_at=auth.now()+timedelta(hours=1)))
        kb = KnowledgeBase(account_id=user.id, owner_id=user.id, name="Idempotent uploads")
        db.add(kb); db.commit(); kb_id = kb.id

    headers = {"X-RAG-Request": "1", "X-CSRF-Token": csrf}
    key = str(uuid4())
    barrier = Barrier(3)

    def create(_):
        with TestClient(main.app, headers=headers) as client:
            client.cookies.set(auth.COOKIE, raw)
            barrier.wait(timeout=10)
            return client.post(
                f"/knowledge-bases/{kb_id}/uploads",
                json={"filename": "sample.txt", "size_bytes": 6},
                headers={"Idempotency-Key": key},
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(create, range(3)))

    assert all(response.status_code == 201 for response in responses), [response.text for response in responses]
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(storage.uploads) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(DocumentUpload).where(
            DocumentUpload.request_key == UUID(key)
        )) == 1


def test_recovery_skips_active_worker_and_does_not_replay_uncertain_paid_batch(auth_postgres):
    from test_retrieval import add_document

    engine = auth_postgres
    with Session(engine) as db:
        user = User(name="Ingestion recovery", email=f"recovery-{uuid4().hex}@example.com")
        db.add(user)
        db.flush()
        kb = KnowledgeBase(account_id=user.id, owner_id=user.id, name="Recovery")
        db.add(kb)
        db.flush()
        safe = add_document(db, kb, "safe resume", status=DocumentStatus.PROCESSING)
        uncertain = add_document(db, kb, "uncertain payment", status=DocumentStatus.PROCESSING)
        active = IngestionJob(document_id=safe.id, phase="parsing", state=JobState.RUNNING)
        ambiguous = IngestionJob(document_id=uncertain.id, phase="indexing", state=JobState.RUNNING,
                                 embedding_submitting=True)
        db.add_all([active, ambiguous])
        db.commit()
        active_id, ambiguous_id = active.id, ambiguous.id
    with Session(engine) as worker, Session(engine) as scanner:
        with worker_claim(worker, active_id) as acquired:
            assert acquired
            recover_interrupted_ingestion(scanner)
            assert scanner.get(IngestionJob, active_id).state == JobState.RUNNING
            paid_job = scanner.get(IngestionJob, ambiguous_id)
            assert paid_job.state == JobState.FAILED
            assert paid_job.failure["code"] == "needs_reconciliation"
        recover_interrupted_ingestion(scanner)
        recovered = scanner.get(IngestionJob, active_id)
        assert recovered.state == JobState.QUEUED and recovered.submission_pending
