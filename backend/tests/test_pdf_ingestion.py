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
    identity = register(owner)
    from app.models import UsagePolicy
    db.add(UsagePolicy(workspace_id=UUID(identity["workspace_id"]), capability="ocr_pages", unit="pages", unit_size=1, credits_per_unit=10))
    db.commit()
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
    settings = tasks.get_settings().model_copy(update={"pdf_parser": "aliyun"})
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

    from app.models import UsageEvent
    events = db.scalars(select(UsageEvent).where(UsageEvent.capability == "ocr_pages")).all()
    assert len(events) == 1
    assert events[0].units == 2


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


def test_pdf_beyond_former_page_cap_preserves_final_page(pdf_job, monkeypatch):
    _, _, db, _, doc_id, job_id, _ = pdf_job
    from app.credits import ensure_account
    account = ensure_account(db, db.get(Document, doc_id).knowledge_base.workspace_id)
    account.balance += 10000
    account.lifetime_granted += 10000
    db.commit()
    writer = PdfWriter()
    for _ in range(501):
        writer.add_blank_page(width=600, height=800)
    content = io.BytesIO()
    writer.write(content)
    monkeypatch.setattr(tasks, "download_fileobj", lambda _: io.BytesIO(content.getvalue()))
    calls = []

    class Provider:
        def __init__(self, *_):
            pass

        def submit(self, content, page_count):
            calls.append(page_count)
            return f"long-pdf-job-{len(calls)}"

        def collect(self, task_id, page_count, check):
            check()
            return normalize_layouts([{"pageNum": page_count - 1, "text": "Last page evidence"}], page_count)

    monkeypatch.setattr(tasks, "AliyunDocMind", Provider)
    tasks.process_document.run(str(job_id))
    assert calls == [500, 1]
    db.expire_all()
    assert db.get(IngestionJob, job_id).state == JobState.SUCCEEDED
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [chunk.page_number for chunk in chunks] == [500, 501]
    assert chunks[-1].text == "Last page evidence"


def test_ingestion_options_are_authenticated_and_never_include_keys(accounts, monkeypatch):
    (owner, _, anonymous), _ = accounts
    assert anonymous.get("/ingestion/options").status_code == 401
    register(owner)
    from app import main
    monkeypatch.setattr(main, "get_settings", lambda: tasks.get_settings().model_copy(update={"pdf_parser": "aliyun"}))
    data = owner.get("/ingestion/options").json()
    assert data == {
        "pdf_parser": "aliyun", "max_pdf_pages": None, "max_upload_bytes": 200 * 1024 * 1024,
        "third_party_processor": "阿里云 DocMind",
    }


@pytest.mark.parametrize("failure", ["timeout", "uncertain", "rejected"])
def test_batch_retry_keeps_completed_results_task_ids_and_original_pages(pdf_job, monkeypatch, failure):
    from app import docmind
    owner, _, db, _, doc_id, job_id, embedded = pdf_job
    monkeypatch.setattr(docmind, "MAX_SUBMISSION_PAGES", 1)
    calls = []
    failed = False

    class Provider:
        def __init__(self, *_):
            pass

        def submit(self, content, page_count):
            nonlocal failed
            number = len([call for call in calls if call[0] == "submit"]) + 1
            calls.append(("submit", number))
            assert page_count == 1
            if number == 2 and failure in ("uncertain", "rejected"):
                failed = True
                raise DocMindError("submission failed", uncertain=failure == "uncertain")
            return f"batch-{number}"

        def collect(self, task_id, page_count, check):
            nonlocal failed
            calls.append(("collect", task_id))
            check()
            if task_id == "batch-2" and failure == "timeout" and not failed:
                failed = True
                raise DocMindError("wait timed out")
            return normalize_layouts([{"pageNum": 0, "text": f"Evidence {task_id}",
                                      "pos": [{"x": 10, "y": 20}, {"x": 30, "y": 40}]}], page_count)

    monkeypatch.setattr(tasks, "AliyunDocMind", Provider)
    with pytest.raises(DocMindError):
        tasks.process_document.run(str(job_id))
    db.expire_all()
    metadata = db.get(Document, doc_id).parsing_metadata
    assert metadata["parts"][0]["status"] == "complete"
    next_id = UUID(owner.post(f"/jobs/{job_id}/retry").json()["id"])
    before_retry = list(calls)
    if failure == "uncertain":
        with pytest.raises(DocMindError, match="阻止重复"):
            tasks.process_document.run(str(next_id))
        assert calls == before_retry
        assert not embedded
        return
    tasks.process_document.run(str(next_id))
    assert calls.count(("collect", "batch-1")) == 1
    assert len([call for call in calls if call[0] == "submit"]) == (2 if failure == "timeout" else 3)
    db.expire_all()
    doc = db.get(Document, doc_id)
    assert doc.parsing_metadata["status"] == "complete"
    assert all("text" not in part for part in doc.parsing_metadata["parts"])
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [chunk.page_number for chunk in chunks] == [1, 2]
    for chunk in chunks:
        assert doc.parsed_text[chunk.char_start:chunk.char_end] == chunk.text
        assert chunk.source_locations[0]["bbox"] == [10, 20, 30, 40]
    assert len(embedded) == 1
    from app.models import UsageEvent
    events = db.scalars(select(UsageEvent).where(UsageEvent.capability == "ocr_pages")).all()
    assert len(events) == 1
    assert events[0].units == 2


@pytest.mark.parametrize("action", ["cancel", "delete"])
def test_batch_interruption_does_not_submit_next_part_or_create_chunks(pdf_job, monkeypatch, action):
    from app import docmind
    owner, _, db, _, doc_id, job_id, embedded = pdf_job
    monkeypatch.setattr(docmind, "MAX_SUBMISSION_PAGES", 1)
    calls = []

    class Provider:
        def __init__(self, *_):
            pass

        def submit(self, content, page_count):
            calls.append("submit")
            return "first-batch"

        def collect(self, task_id, page_count, check):
            calls.append("collect")
            response = (owner.post(f"/jobs/{job_id}/cancel") if action == "cancel"
                        else owner.delete(f"/documents/{doc_id}"))
            assert response.status_code == 200
            check()
            raise AssertionError("Cancelled work continued")

    monkeypatch.setattr(tasks, "AliyunDocMind", Provider)
    tasks.process_document.run(str(job_id))
    db.expire_all()
    assert calls == ["submit", "collect"]
    assert db.get(IngestionJob, job_id).state == JobState.CANCELLED
    assert db.get(Document, doc_id).parsed_text is None
    assert not embedded
