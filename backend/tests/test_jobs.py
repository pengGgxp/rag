from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from app.auth import now
from app.models import Chunk, DocumentStatus, EmbeddingStatus, IngestionJob, JobState, KnowledgeBase
from test_auth import accounts, library, register, share, switch
from test_retrieval import add_document


def test_failed_job_can_be_retried_and_cancelled_job_stays_terminal(accounts, monkeypatch):
    (owner, viewer, _), db = accounts
    register(owner)
    register(viewer, "viewer@example.com", "Viewer")
    kb_id = library(owner)
    document = add_document(db, db.get(KnowledgeBase, kb_id), "retryable")
    document.status = DocumentStatus.FAILED
    failed = IngestionJob(document_id=document.id, state=JobState.FAILED, error_detail="parse failed")
    db.add(failed)
    db.commit()
    queued = []
    monkeypatch.setattr("app.knowledge.routes.process_document", type("Task", (), {
        "delay": lambda _self, job_id: queued.append(job_id),
    })())

    retry = owner.post(f"/jobs/{failed.id}/retry")
    assert retry.status_code == 202, retry.text
    new_job = retry.json()
    assert new_job["id"] != str(failed.id)
    assert new_job["state"] == "queued"
    assert queued == [new_job["id"]]
    replay = owner.post(f"/jobs/{failed.id}/retry")
    assert replay.status_code == 202
    assert replay.json()["id"] == new_job["id"]
    assert queued == [new_job["id"]]
    db.expire_all()
    assert db.get(IngestionJob, failed.id).state == JobState.FAILED

    cancel = owner.post(f"/jobs/{new_job['id']}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "cancelled"
    assert owner.post(f"/jobs/{new_job['id']}/cancel").status_code == 409
    assert owner.post(f"/jobs/{failed.id}/retry").status_code == 202

    assert viewer.post(f"/jobs/{new_job['id']}/cancel").status_code == 404


def test_retry_recovers_abandoned_running_chunks(accounts, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    kb_id = library(owner)
    document = add_document(db, db.get(KnowledgeBase, kb_id), "retryable")
    document.parsed_text = "retryable"
    document.status = DocumentStatus.FAILED
    chunk = document.chunks[0]
    chunk.embedding = None
    chunk.embedding_status = EmbeddingStatus.RUNNING
    failed = IngestionJob(document_id=document.id, state=JobState.FAILED, error_detail="indexing stopped")
    db.add(failed)
    db.commit()
    dispatched = []
    monkeypatch.setattr("app.knowledge.routes.embed_document", type("Task", (), {
        "delay": lambda _self, *args: dispatched.append(args),
    })())

    response = owner.post(f"/jobs/{failed.id}/retry")

    assert response.status_code == 202, response.text
    assert response.json()["phase"] == "indexing"
    assert dispatched == [(str(document.id), response.json()["id"])]
    db.refresh(chunk)
    assert chunk.embedding_status == EmbeddingStatus.FAILED
    assert chunk.embedding_error == "上一次索引任务未完成，请重试"


def test_manual_retry_cannot_create_successor_during_automatic_backoff(accounts):
    (owner, _, _), db = accounts
    register(owner)
    library_id = library(owner)
    document = add_document(db, db.get(KnowledgeBase, library_id), "processing", status=DocumentStatus.PROCESSING)
    job = IngestionJob(document_id=document.id, phase="parsing", state=JobState.QUEUED,
                       retry_attempts=1, retry_after=now() + timedelta(seconds=30), submission_pending=True)
    db.add(job)
    db.commit()

    for _ in range(3):
        assert owner.post(f"/jobs/{job.id}/retry").status_code == 409
    assert db.scalars(select(IngestionJob).where(IngestionJob.document_id == document.id)).all() == [job]
