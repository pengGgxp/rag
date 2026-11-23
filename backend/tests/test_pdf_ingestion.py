import hashlib
import io
from uuid import UUID

import pytest
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tasks
from app.knowledge.ingestion import pipeline
from app.knowledge import routes as knowledge_routes
from app.knowledge.ingestion.docmind import DocMindError, normalize_layouts
from app.models import (
    Chunk, Document, DocumentStatus, EmbeddingStatus, IngestionJob, JobState,
    KnowledgeBase, UsageEvent, UsagePolicy,
)
from test_auth import library, register
from test_retrieval import add_document
from pdf_fixtures import make_pdf


@pytest.fixture
def pdf_job(accounts, monkeypatch):
    (owner, outsider, _), db = accounts
    identity = register(owner)
    from app.models import UsagePolicy
    db.add(UsagePolicy(account_id=UUID(identity["user"]["id"]), capability="ocr_pages", unit="pages", unit_size=1, credits_per_unit=10))
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
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    store.objects[doc.object_key] = content.getvalue()
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    doc.size_bytes = len(content.getvalue())
    doc.content_hash = hashlib.sha256(content.getvalue()).hexdigest()
    db.commit()
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(pipeline, "download_fileobj", lambda _: io.BytesIO(content.getvalue()))
    settings = pipeline.get_settings().model_copy(update={"pdf_parser": "aliyun"})
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    embedded = []
    monkeypatch.setattr(tasks.embed_document, "delay", lambda *args: embedded.append(args))
    monkeypatch.setattr("app.knowledge.routes.process_document.delay", lambda *_: None)
    return owner, outsider, db, kb, doc.id, job.id, embedded


def fake_provider(monkeypatch, *, collect=None, submit=None, original=None):
    calls = []

    class Provider:
        def __init__(self, *_):
            pass

        def submit(self, content, page_count):
            if original is not None:
                assert content == original
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

    monkeypatch.setattr(pipeline, "AliyunDocMind", Provider)
    return calls


@pytest.mark.parametrize("interrupt", [None, "rejected", "commit_failed", "response_lost"])
def test_large_pdf_index_retry_batches_without_reparsing_or_duplicate_billing(pdf_job, monkeypatch, interrupt):
    import httpx
    from app.config import Settings
    from app.knowledge.ingestion.embedding import OpenAICompatibleEmbeddingProvider

    owner, _, db, kb_id, doc_id, old_job_id, embedded = pdf_job
    document = db.get(Document, doc_id)
    document.parsed_text = "previously parsed PDF"
    document.status = DocumentStatus.FAILED
    document.chunks.clear()
    document.chunks.extend(Chunk(
        ordinal=index, text=str(index), token_count=10, char_start=index,
        char_end=index + 1, content_hash=str(index), embedding_status=EmbeddingStatus.FAILED,
    ) for index in range(114))
    db.get(IngestionJob, old_job_id).state = JobState.FAILED
    db.add(UsagePolicy(
        account_id=db.get(KnowledgeBase, kb_id).account_id, capability="embedding_tokens",
        unit="tokens", unit_size=1000, credits_per_unit=1,
    ))
    db.commit()
    chunk_ids = [chunk.id for chunk in document.chunks]
    settings = Settings(
        embedding_base_url="https://provider.example/v1", embedding_api_key="test",
        embedding_model="test-model", embedding_dimension=1024,
    )
    monkeypatch.setattr(pipeline, "OpenAICompatibleEmbeddingProvider",
                        lambda: OpenAICompatibleEmbeddingProvider(settings))
    batches = []
    checkpoint_seen = []
    commit = Session.commit
    fail_vector_commit = False

    def commit_with_fault(session):
        nonlocal fail_vector_commit
        if fail_vector_commit and any(isinstance(row, Chunk) for row in session.dirty):
            fail_vector_commit = False
            raise OSError("vector checkpoint failed")
        commit(session)

    def fake_post(url, **kwargs):
        nonlocal fail_vector_commit
        inputs = kwargs["json"]["input"]
        assert len(inputs) <= 20
        batches.append(inputs)
        checkpoint_seen.append(db.get(IngestionJob, job_id).embedding_submitting)
        if inputs[0] == "20" and len(batches) == 2:
            if interrupt == "rejected":
                return httpx.Response(429, headers={"Retry-After": "600"}, json={})
            if interrupt == "commit_failed":
                fail_vector_commit = True
            if interrupt == "response_lost":
                raise httpx.ReadTimeout("response lost after submission")
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(text)] + [0.0] * 1023}
            for index, text in reversed(list(enumerate(inputs)))
        ]})

    monkeypatch.setattr(httpx, "post", fake_post)
    response = owner.post(f"/jobs/{old_job_id}/retry")
    assert response.status_code == 202
    job_id = UUID(response.json()["id"])
    assert embedded == [(str(doc_id), str(job_id))]
    monkeypatch.setattr(Session, "commit", commit_with_fault)
    if interrupt:
        from app.knowledge.ingestion.embedding import EmbeddingProviderError

        with pytest.raises((EmbeddingProviderError, OSError)):
            tasks.embed_document.run(doc_id, job_id)
        db.expire_all()
        job = db.get(IngestionJob, job_id)
        assert job.state == JobState.FAILED
        assert db.scalar(select(Chunk).where(Chunk.document_id == doc_id, Chunk.ordinal == 0)).embedding[0] == 0.0
        retry = owner.post(f"/jobs/{job_id}/retry")
        if interrupt != "rejected":
            assert job.embedding_submitting
            assert retry.status_code == 409
            assert retry.json()["detail"]["code"] == "needs_reconciliation"
            tasks.embed_document.run(doc_id, job_id)
            assert len(batches) == 2 and all(checkpoint_seen)
            assert db.scalars(select(UsageEvent)).all() == []
            return
        assert not job.embedding_submitting
        assert retry.status_code == 202
        job_id = UUID(retry.json()["id"])
    tasks.embed_document.run(doc_id, job_id)
    tasks.embed_document.run(doc_id, job_id)
    db.expire_all()
    document = db.get(Document, doc_id)
    assert document.status == DocumentStatus.READY
    assert db.get(IngestionJob, job_id).state == JobState.SUCCEEDED
    assert all(checkpoint_seen)
    assert [batch[0] for batch in batches] == (
        ["0", "20", "20", "40", "60", "80", "100"] if interrupt else ["0", "20", "40", "60", "80", "100"]
    )
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [chunk.id for chunk in chunks] == chunk_ids
    assert [chunk.embedding[0] for chunk in chunks] == list(range(114))
    events = db.scalars(select(UsageEvent)).all()
    assert len(events) == 1
    from decimal import Decimal
    assert (events[0].capability, events[0].units, events[0].credits) == ("embedding_tokens", 1140, Decimal("1.1400"))


def test_cos_pdf_is_submitted_by_url_and_resume_reuses_paid_task(pdf_job, monkeypatch):
    owner, _, db, _, doc_id, job_id, _ = pdf_job
    from types import SimpleNamespace
    calls = []
    class Provider:
        def __init__(self, *_):
            pass
        def submit_url(self, url, pages):
            calls.append((url, pages))
            return "cos-url-job"
        def collect(self, identifier, count, check):
            check()
            return normalize_layouts([{"pageNum": 0, "text": "URL parsed document", "type": "text"}], count)
    monkeypatch.setattr(pipeline, "ocr_storage", lambda _: SimpleNamespace(read_url=lambda key: "https://cos.invalid/" + key))
    monkeypatch.setattr(pipeline, "AliyunDocMind", Provider)
    tasks.process_document.run(str(job_id))
    tasks.process_document.run(str(job_id))
    assert len(calls) == 1 and calls[0][1] == 2
    assert "https://" not in str(db.get(Document, doc_id).parsing_metadata)


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


@pytest.mark.parametrize("parser", ["local", "aliyun"])
@pytest.mark.parametrize("password", [None, ""], ids=["plain", "empty-password-aes128"])
def test_pdf_ingestion_preserves_original_version_and_single_processing(pdf_job, monkeypatch, parser, password):
    import hashlib

    owner, outsider, db, _, doc_id, job_id, embedded = pdf_job
    content = make_pdf(password=password)
    monkeypatch.setattr(pipeline, "download_fileobj", lambda _: io.BytesIO(content))
    document = db.get(Document, doc_id)
    document.content_hash = hashlib.sha256(content).hexdigest()
    document.size_bytes = len(content)
    db.commit()
    settings = pipeline.get_settings().model_copy(update={"pdf_parser": parser})
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    document = db.get(Document, doc_id)
    document.size_bytes, document.content_hash = len(content), hashlib.sha256(content).hexdigest()
    db.commit()
    def identity(document):
        return (document.object_key, document.size_bytes, document.content_hash, document.version_group_id,
                document.version_number, document.supersedes_id, document.is_active)
    before = identity(document)
    calls = fake_provider(monkeypatch, original=content)
    tasks.process_document.run(str(job_id))
    tasks.process_document.run(str(job_id))
    db.expire_all()
    document = db.get(Document, doc_id)
    assert identity(document) == before
    assert db.get(IngestionJob, job_id).state == JobState.SUCCEEDED
    assert len(embedded) == 1
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [chunk.page_number for chunk in chunks] == [1, 2]
    for chunk in chunks:
        assert document.parsed_text[chunk.char_start:chunk.char_end] == chunk.text
    assert outsider.get(f"/jobs/{job_id}").status_code == 404
    assert outsider.post(f"/jobs/{job_id}/retry").status_code == 404
    events = db.scalars(select(UsageEvent).where(UsageEvent.capability == "ocr_pages")).all()
    assert len(events) == (1 if parser == "aliyun" else 0)
    assert calls == ([("submit", 2), ("collect", "known-remote-job")] if parser == "aliyun" else [])
    if events:
        assert events[0].units == 2


@pytest.mark.parametrize("parser", ["local", "aliyun"])
@pytest.mark.parametrize("content,reason", [
    (make_pdf(password="secret"), "需要打开密码"),
    (b"%PDF-1.7\nbroken", "已损坏或格式无效"),
    (b"", "为空或没有页面"),
    (make_pdf(page_count=0), "为空或没有页面"),
], ids=["password-required", "broken", "empty", "no-pages"])
def test_pdf_failure_reason_survives_retry_without_paid_work(pdf_job, monkeypatch, parser, content, reason):
    from app.models import UserCreditAccount, CreditLedgerEntry

    owner, _, db, _, doc_id, job_id, embedded = pdf_job
    monkeypatch.setattr(pipeline, "download_fileobj", lambda _: io.BytesIO(content))
    document = db.get(Document, doc_id)
    document.content_hash = hashlib.sha256(content).hexdigest()
    document.size_bytes = len(content)
    db.commit()
    settings = pipeline.get_settings().model_copy(update={"pdf_parser": parser})
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    before = [(row.user_id, row.balance, row.lifetime_spent) for row in db.scalars(select(UserCreditAccount))]
    ledger_before = list(db.scalars(select(CreditLedgerEntry.id)))
    calls = fake_provider(monkeypatch)
    with pytest.raises(ValueError, match=reason):
        tasks.process_document.run(str(job_id))
    db.expire_all()
    document = db.get(Document, doc_id)
    assert document.status == DocumentStatus.FAILED
    assert document.error_detail == db.get(IngestionJob, job_id).error_detail
    job_response = owner.get(f"/jobs/{job_id}").json()
    assert job_response["error_detail"] == job_response["failure"]["message"]
    assert reason in job_response["error_detail"]
    listed = owner.get(f"/knowledge-bases/{document.knowledge_base_id}/documents").json()[0]
    assert listed["error_detail"] == listed["failure"]["message"]
    preview = owner.get(f"/documents/{doc_id}/preview").json()
    assert preview["error_detail"] == preview["failure"]["message"]
    next_id = UUID(owner.post(f"/jobs/{job_id}/retry").json()["id"])
    with pytest.raises(ValueError, match=reason):
        tasks.process_document.run(str(next_id))
    db.expire_all()
    assert db.get(IngestionJob, next_id).state == JobState.FAILED
    assert reason in db.get(Document, doc_id).error_detail
    assert not calls and not embedded and not db.scalars(select(UsageEvent)).all()
    assert [(row.user_id, row.balance, row.lifetime_spent) for row in db.scalars(select(UserCreditAccount))] == before
    assert list(db.scalars(select(CreditLedgerEntry.id))) == ledger_before


@pytest.mark.parametrize("password", [None, ""], ids=["plain", "empty-password-aes128"])
def test_timeout_retry_reuses_remote_task_instead_of_submitting_again(pdf_job, monkeypatch, password):
    owner, _, db, _, doc_id, job_id, embedded = pdf_job
    content = make_pdf(password=password)
    monkeypatch.setattr(pipeline, "download_fileobj", lambda _: io.BytesIO(content))
    document = db.get(Document, doc_id)
    document.content_hash = hashlib.sha256(content).hexdigest()
    document.size_bytes = len(content)
    db.commit()

    def timeout(_):
        raise DocMindError("wait timed out")

    calls = fake_provider(monkeypatch, collect=timeout, original=content)
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
    retry = owner.post(f"/jobs/{job_id}/retry")
    assert retry.status_code == 409
    assert calls == [("submit", 2)]
    db.expire_all()
    assert db.get(Document, doc_id).parsing_metadata["status"] == "submitting"
    failure = db.get(IngestionJob, job_id).failure
    assert failure["code"] == "needs_reconciliation" and not failure["retryable"]


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
    account = ensure_account(db, db.get(Document, doc_id).knowledge_base.account_id)
    account.balance += 10000
    account.lifetime_granted += 10000
    db.commit()
    writer = PdfWriter()
    for _ in range(501):
        writer.add_blank_page(width=600, height=800)
    content = io.BytesIO()
    writer.write(content)
    monkeypatch.setattr(pipeline, "download_fileobj", lambda _: io.BytesIO(content.getvalue()))
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

    monkeypatch.setattr(pipeline, "AliyunDocMind", Provider)
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
    monkeypatch.setattr(knowledge_routes, "get_settings", lambda: pipeline.get_settings().model_copy(update={"pdf_parser": "aliyun"}))
    data = owner.get("/ingestion/options").json()
    assert data == {
        "pdf_parser": "aliyun", "max_pdf_pages": None, "max_upload_bytes": 200 * 1024 * 1024,
        "third_party_processor": "阿里云 DocMind",
    }


@pytest.mark.parametrize("failure", ["timeout", "uncertain", "rejected"])
def test_batch_retry_keeps_completed_results_task_ids_and_original_pages(pdf_job, monkeypatch, failure):
    from app.knowledge.ingestion import docmind
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

    monkeypatch.setattr(pipeline, "AliyunDocMind", Provider)
    with pytest.raises(DocMindError):
        tasks.process_document.run(str(job_id))
    db.expire_all()
    metadata = db.get(Document, doc_id).parsing_metadata
    assert metadata["parts"][0]["status"] == "complete"
    before_retry = list(calls)
    if failure == "uncertain":
        retry = owner.post(f"/jobs/{job_id}/retry")
        assert retry.status_code == 409
        assert calls == before_retry
        assert not embedded
        return
    next_id = UUID(owner.post(f"/jobs/{job_id}/retry").json()["id"])
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
    from app.knowledge.ingestion import docmind
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

    monkeypatch.setattr(pipeline, "AliyunDocMind", Provider)
    tasks.process_document.run(str(job_id))
    db.expire_all()
    assert calls == ["submit", "collect"]
    assert db.get(IngestionJob, job_id).state == JobState.CANCELLED
    assert db.get(Document, doc_id).parsed_text is None
    assert not embedded
