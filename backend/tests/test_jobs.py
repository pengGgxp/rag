from uuid import UUID

from sqlalchemy import select

from app.models import DocumentStatus, IngestionJob, JobState, KnowledgeBase
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
    monkeypatch.setattr("app.main.process_document", type("Task", (), {
        "delay": lambda _self, job_id: queued.append(job_id),
    })())

    retry = owner.post(f"/jobs/{failed.id}/retry")
    assert retry.status_code == 202, retry.text
    new_job = retry.json()
    assert new_job["id"] != str(failed.id)
    assert new_job["state"] == "queued"
    assert queued == [new_job["id"]]
    db.expire_all()
    assert db.get(IngestionJob, failed.id).state == JobState.FAILED

    cancel = owner.post(f"/jobs/{new_job['id']}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "cancelled"
    assert owner.post(f"/jobs/{new_job['id']}/cancel").status_code == 409
    assert owner.post(f"/jobs/{failed.id}/retry").status_code == 202

    assert viewer.post(f"/jobs/{new_job['id']}/cancel").status_code == 404
