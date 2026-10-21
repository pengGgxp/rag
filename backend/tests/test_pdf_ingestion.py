import io
from uuid import UUID

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tasks
from app.docmind import DocMindError, normalize_layouts
from app.models import Chunk, Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase
from test_auth import library, register
from test_retrieval import add_document


@pytest.fixture
def pdf_job(accounts, monkeypatch):
    (owner, outsider, _), db = accounts
    register(owner)
    register(outsider, "outsider@example.com")
    kb = library(owner)
    doc = add_document(db, db.get(KnowledgeBase, kb), "old chunk")
    doc.filename = "mixed.pdf"
    doc.content_type = "application/pdf"
    doc.status = DocumentStatus.QUEUED
    doc.parsed_text = None
    job = IngestionJob(document_id=doc.id)
    db.add(job)
    db.commit()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    content = io.BytesIO()
    writer.write(content)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(tasks, "download_fileobj", lambda _: io.BytesIO(content.getvalue()))
    settings = tasks.get_settings().model_copy(update={"pdf_parser": "aliyun", "docmind_max_pages": 50})
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    embedded = []
    monkeypatch.setattr(tasks.embed_document, "delay", lambda *args: embedded.append(args))
    monkeypatch.setattr("app.main.process_document.delay", lambda *_: None)
    return owner, outsider, db, kb, doc.id, job.id, embedded


def fake_provider(monkeypatch, *, collect=None, submit=None):
    calls = []

    class Provider:
        def __init__(self, *_):
            pass

        def submit(self, content, page_count):
            calls.append(("submit", page_count))
            return submit() if submit else "known-remote-job"

        def collect(self, task_id, page_count, check):
            calls.append(("collect", task_id))
            if collect:
                collect(check)
            check()
            return normalize_layouts([
                {"pageNum": 0, "index": 0, "type": "text", "text": "needle digital page"},
                {"pageNum": 1, "index": 1, "type": "text", "text": "needle scanned page",
                 "pos": [{"x": 10, "y": 20}, {"x": 120, "y": 40}]},
            ], 2)

    monkeypatch.setattr(tasks, "AliyunDocMind", Provider)
    return calls


def test_mixed_pdf_ingestion_is_idempotent_and_page_metadata_reaches_retrieval(pdf_job, monkeypatch):
    owner, outsider, db, kb, doc_id, job_id, embedded = pdf_job
    calls = fake_provider(monkeypatch)
    tasks.process_document.run(str(job_id))
    tasks.process_document.run(str(job_id))
    db.expire_all()
    assert db.get(IngestionJob, job_id).state == JobState.SUCCEEDED
    assert len(embedded) == 1
    assert calls == [("submit", 2), ("collect", "known-remote-job")]
    doc = db.get(Document, doc_id)
    assert doc.parsing_metadata["status"] == "complete"
    assert doc.parser_name == "aliyun-docmind-v1"
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [row.page_number for row in chunks] == [1, 2]
    doc.status = DocumentStatus.READY
    db.commit()
    results = owner.post(f"/knowledge-bases/{kb}/retrieval", json={"query": "needle"}).json()["results"]
    assert {row["chunk"]["page_number"] for row in results} == {1, 2}
    assert any(row["chunk"]["source_locations"][0]["bbox"] == [10, 20, 120, 40] for row in results)
    assert outsider.get(f"/documents/{doc_id}/chunks").status_code == 404
    assert outsider.post(f"/knowledge-bases/{kb}/retrieval", json={"query": "needle"}).status_code == 404
    preview = owner.get(f"/documents/{doc_id}/preview").json()
    assert "known-remote-job" not in str(preview)
    assert "parsing_metadata" not in preview
    assert owner.delete(f"/documents/{doc_id}").status_code == 200
    assert owner.post(f"/knowledge-bases/{kb}/retrieval", json={"query": "needle"}).json()["results"] == []


def test_timeout_retry_reuses_remote_task_instead_of_submitting_again(pdf_job, monkeypatch):
    owner, _, db, _, doc_id, job_id, embedded = pdf_job

    def timeout(_):
        raise DocMindError("wait timed out")

    calls = fake_provider(monkeypatch, collect=timeout)
    with pytest.raises(DocMindError):
        tasks.process_document.run(str(job_id))
    db.expire_all()
    assert db.get(Document, doc_id).parsing_metadata["task_id"] == "known-remote-job"
    assert db.get(IngestionJob, job_id).state == JobState.FAILED
    next_id = UUID(owner.post(f"/jobs/{job_id}/retry").json()["id"])
    reused = fake_provider(monkeypatch)
    tasks.process_document.run(str(next_id))
    assert calls == [("submit", 2), ("collect", "known-remote-job")]
    assert reused == [("collect", "known-remote-job")]
    assert len(embedded) == 1


@pytest.mark.parametrize("action", ["cancel", "delete"])
def test_cancellation_or_deletion_during_polling_never_revives_document(pdf_job, monkeypatch, action):
    owner, _, db, _, doc_id, job_id, embedded = pdf_job

    def interrupt(check):
        if action == "cancel":
            assert owner.post(f"/jobs/{job_id}/cancel").status_code == 200
        else:
            assert owner.delete(f"/documents/{doc_id}").status_code == 200
        check()

    fake_provider(monkeypatch, collect=interrupt)
    tasks.process_document.run(str(job_id))
    db.expire_all()
    assert db.get(IngestionJob, job_id).state == JobState.CANCELLED
    assert db.get(Document, doc_id).status == (
        DocumentStatus.DELETED if action == "delete" else DocumentStatus.FAILED
    )
    assert not embedded
    assert db.get(Document, doc_id).parsed_text is None


def test_ambiguous_submission_is_not_paid_for_again_on_retry(pdf_job, monkeypatch):
    owner, _, db, _, doc_id, job_id, _ = pdf_job

    def uncertain():
        raise DocMindError("submission timeout", uncertain=True)

    calls = fake_provider(monkeypatch, submit=uncertain)
    with pytest.raises(DocMindError):
        tasks.process_document.run(str(job_id))
    next_id = UUID(owner.post(f"/jobs/{job_id}/retry").json()["id"])
    with pytest.raises(DocMindError, match="阻止重复"):
        tasks.process_document.run(str(next_id))
    assert calls == [("submit", 2)]
    db.expire_all()
    assert db.get(Document, doc_id).parsing_metadata["status"] == "submitting"


def test_completed_parse_cache_is_reused_on_retry(pdf_job, monkeypatch):
    owner, _, db, _, doc_id, job_id, _ = pdf_job
    calls = fake_provider(monkeypatch)
    tasks.process_document.run(str(job_id))
    db.expire_all()
    doc = db.get(Document, doc_id)
    doc.status = DocumentStatus.FAILED
    for job in db.scalars(select(IngestionJob).where(IngestionJob.document_id == doc_id)).all():
        job.state = JobState.SUCCEEDED
    new_job = IngestionJob(document_id=doc_id, state=JobState.FAILED)
    db.add(new_job)
    db.commit()
    next_id = UUID(owner.post(f"/jobs/{new_job.id}/retry").json()["id"])
    tasks.process_document.run(str(next_id))
    assert calls == [("submit", 2), ("collect", "known-remote-job")]


def test_page_limit_prevents_any_cloud_request(pdf_job, monkeypatch):
    _, _, db, _, _, job_id, _ = pdf_job
    settings = tasks.get_settings().model_copy(update={"docmind_max_pages": 1})
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    calls = fake_provider(monkeypatch)
    with pytest.raises(DocMindError, match="未提交云端"):
        tasks.process_document.run(str(job_id))
    assert not calls
    db.expire_all()
    assert db.get(IngestionJob, job_id).state == JobState.FAILED


def test_ingestion_options_are_authenticated_and_never_include_keys(accounts, monkeypatch):
    (owner, _, anonymous), _ = accounts
    assert anonymous.get("/ingestion/options").status_code == 401
    register(owner)
    from app import main
    monkeypatch.setattr(main, "get_settings", lambda: tasks.get_settings().model_copy(update={"pdf_parser": "aliyun"}))
    data = owner.get("/ingestion/options").json()
    assert data == {
        "pdf_parser": "aliyun", "max_pdf_pages": 50, "third_party_processor": "阿里云 DocMind",
    }
