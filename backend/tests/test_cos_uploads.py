import hashlib
import io
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, document_uploads as uploads, site_auth, storage_settings, tasks
from app.cos_storage import CosStorage
from app.models import (
    Chunk, CreditAccount, CreditLedgerEntry, Document, DocumentStatus, DocumentUpload, EmbeddingStatus,
    IngestionJob, JobState, Membership, SiteAuditEvent, SiteStorageSettings, UsageEvent, UsagePolicy, User,
)
from test_auth import library, register, share, switch


class FakeCos:
    def __init__(self):
        self.objects, self.uploads = {}, {}
        self.seals = self.downloads = 0

    def begin(self, key, _):
        identifier = str(uuid4())
        self.uploads[identifier] = {"key": key, "parts": {}}
        return identifier

    def parts(self, key, identifier):
        return [{"PartNumber": n, "Size": len(body), "ETag": f'"part-{n}"'}
            for n, body in self.uploads[identifier]["parts"].items()]

    def part_url(self, key, identifier, number, size):
        return f"https://cos.invalid/{identifier}/{number}?expected={size}"

    def complete(self, key, identifier, parts):
        self.seals += 1
        self.objects[key] = b"".join(self.uploads[identifier]["parts"][part["PartNumber"]] for part in parts)
        del self.uploads[identifier]

    def head(self, key):
        return {"Content-Length": len(self.objects[key])} if key in self.objects else None

    def delete(self, key):
        self.objects.pop(key, None)

    def abort(self, key, identifier):
        self.uploads.pop(identifier, None)

    def download(self, key):
        self.downloads += 1
        return io.BytesIO(self.objects[key])

    def download_range(self, key, start, end):
        return io.BytesIO(self.objects[key][start:end+1])

    def put(self, key, stream, _=None):
        self.objects[key] = stream.read()

    def incomplete_uploads(self):
        return []

    def iter_objects(self):
        return []

    def _call(self, name, **kwargs):
        assert name == "upload_part"
        self.uploads[kwargs["UploadId"]]["parts"][kwargs["PartNumber"]] = kwargs["Body"]
        return {"ETag": '"test-part"'}

    def upload_part(self, key, identifier, number, body):
        return self._call("upload_part", Key=key, UploadId=identifier, PartNumber=number, Body=body)["ETag"]


@pytest.fixture
def direct(accounts, monkeypatch):
    (owner, outsider, _), db = accounts
    identity = register(owner)
    register(outsider, "outsider@example.com")
    kb = library(owner)
    storage = FakeCos()
    monkeypatch.setattr(uploads, "current_storage", lambda *_: storage)
    monkeypatch.setattr(uploads, "SessionLocal", lambda: Session(db.get_bind(), expire_on_commit=False))
    queued = []
    monkeypatch.setattr(tasks.verify_upload, "delay", queued.append)
    processed = []
    monkeypatch.setattr(tasks.process_document, "run", lambda *args, **kwargs: processed.append((args, kwargs)))
    return owner, outsider, db, identity, kb, storage, queued, processed


def create(owner, kb, content=b"document", name="document.txt", **kwargs):
    response = owner.post(f"/knowledge-bases/{kb}/uploads", json={
        "filename": name, "size_bytes": len(content), **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def send(owner, db, storage, upload, content):
    row = db.get(DocumentUpload, UUID(upload["id"]))
    for number in range(1, (len(content) + uploads.PART_SIZE - 1) // uploads.PART_SIZE + 1):
        body = content[(number - 1)*uploads.PART_SIZE:number*uploads.PART_SIZE]
        result = owner.post(f"/uploads/{row.id}/parts/{number}", json={"sha256": hashlib.sha256(body).hexdigest()})
        assert result.status_code == 200, result.text
        assert result.json()["size"] == len(body)
        storage.uploads[row.multipart_id]["parts"][number] = body
    return row


def test_direct_completion_admission_is_idempotent_and_reuses_download(direct):
    owner, _, db, _, kb, storage, queued, processed = direct
    content = b"test document content"
    upload = create(owner, kb, content)
    row = send(owner, db, storage, upload, content)
    for _ in range(2):
        result = owner.post(f"/uploads/{row.id}/complete")
        assert result.status_code == 202, result.text
    assert storage.seals == 1 and len(queued) == 2
    uploads.finalize_upload(str(row.id))
    uploads.finalize_upload(str(row.id))
    db.expire_all()
    row = db.get(DocumentUpload, row.id)
    assert row.state == "completed"
    document = db.get(Document, row.document_id)
    assert document.content_hash == hashlib.sha256(content).hexdigest()
    assert storage.downloads == 1
    assert len(processed) == 1 and processed[0][1]["prepared_content"] == content
    assert owner.get(f"/uploads/{row.id}").json()["document_status"] == "queued"
    assert db.scalar(select(func.count()).select_from(IngestionJob)) == 1
    assert owner.post(f"/uploads/{row.id}/complete").json()["document_id"] == str(document.id)


@pytest.mark.parametrize("outcome", ["recover", "exhaust", "cancel", "delete", "settlement_error"])
def test_upload_embedding_retry_progress_and_billing(direct, monkeypatch, outcome):
    from decimal import Decimal
    import httpx
    from app.config import Settings
    from app.embedding import EmbeddingProviderError, OpenAICompatibleEmbeddingProvider
    from app.upstream_retries import RetryBudget

    owner, outsider, db, identity, kb, storage, _, _ = direct
    workspace_id = UUID(identity["workspace_id"])
    payload = b"retry document"
    row = send(owner, db, storage, create(owner, kb, payload), payload)
    upload_id = str(row.id)
    assert owner.post(f"/uploads/{upload_id}/complete").status_code == 202
    uploads.finalize_upload(upload_id)
    db.expire_all()
    doc_id = row.document_id
    document = db.get(Document, doc_id)
    document.parsed_text = payload.decode()
    old_job = document.jobs[0]
    old_job.state = JobState.SUCCEEDED
    job = IngestionJob(document_id=doc_id, created_at=old_job.created_at)
    db.add(job)
    document.chunks.extend(Chunk(
        ordinal=i, text=str(i), token_count=10, char_start=i, char_end=i + 1,
        content_hash=str(i),
    ) for i in range(21))
    db.add(UsagePolicy(workspace_id=workspace_id, capability="embedding_tokens",
                       unit="tokens", unit_size=1000, credits_per_unit=1))
    db.commit()
    job_id = job.id
    balance = db.get(CreditAccount, workspace_id).balance

    settings = Settings(embedding_base_url="https://embedding.invalid", embedding_api_key="test",
                        embedding_model="test-model")
    monkeypatch.setattr(tasks, "OpenAICompatibleEmbeddingProvider", lambda: OpenAICompatibleEmbeddingProvider(settings))
    monkeypatch.setattr(tasks, "SessionLocal", lambda: Session(db.get_bind()))
    dispatched = []
    monkeypatch.setattr(tasks.embed_document, "delay", lambda *args: dispatched.append(args))
    calls, seen = [], []
    manual_retry = False

    def wait(_budget, _seconds):
        db.expire_all()
        upload = owner.get(f"/uploads/{upload_id}").json()
        detail = owner.get(f"/jobs/{job_id}").json()
        assert upload["document_status"] == "processing" and not upload["document_error"]
        assert detail["state"] == "running" and not detail["error_detail"]
        seen.append(upload["retry_progress"])
        assert detail["retry_progress"] == seen[-1]
        documents = owner.get(f"/knowledge-bases/{kb}/documents").json()
        assert documents[0]["job"]["retry_progress"] == seen[-1]
        assert owner.post(f"/jobs/{job_id}/retry").status_code == 409
        assert outsider.get(f"/uploads/{upload_id}").status_code == 404
        tasks.embed_document.run(doc_id, job_id)
        if outcome == "cancel":
            assert owner.post(f"/jobs/{job_id}/cancel").status_code == 200
        elif outcome == "delete":
            assert owner.delete(f"/documents/{doc_id}").status_code == 200

    def post(_url, *, json, **_):
        inputs = json["input"]
        calls.append(inputs[0])
        if not manual_retry and inputs[0] == "20" and (
            outcome in ("exhaust", "cancel", "delete") or
            outcome == "recover" and len(calls) <= 4
        ):
            return httpx.Response(429, json={"error": "private provider response"})
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [float(text)] + [0.0] * 1023}
            for i, text in reversed(list(enumerate(inputs)))
        ]})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(RetryBudget, "wait", wait)
    if outcome == "settlement_error":
        def fail_settlement(*_, **__):
            raise RuntimeError("database write failed")
        monkeypatch.setattr(tasks, "settle_usage", fail_settlement)
        with pytest.raises(RuntimeError, match="database write failed"):
            tasks.embed_document.run(doc_id, job_id)
    elif outcome == "exhaust":
        with pytest.raises(EmbeddingProviderError):
            tasks.embed_document.run(doc_id, job_id)
    else:
        tasks.embed_document.run(doc_id, job_id)
    tasks.embed_document.run(doc_id, job_id)

    assert calls == ["0"] + ["20"] * (4 if outcome in ("recover", "exhaust") else 1)
    retries = {"recover": 3, "exhaust": 3, "cancel": 1, "delete": 1}.get(outcome, 0)
    assert seen == [{"number": n, "maximum": 3} for n in range(1, retries + 1)]
    db.expire_all()
    upload = owner.get(f"/uploads/{upload_id}").json()
    assert upload["document_status"] == {"recover": "ready", "delete": "deleted"}.get(outcome, "failed")
    assert not upload.get("retry_progress")
    if outcome == "exhaust":
        assert upload["document_error"] == "处理服务暂时繁忙，请稍后重试"
        listed = owner.get(f"/knowledge-bases/{kb}/documents").json()[0]
        assert listed["job"]["state"] == "failed"
        assert listed["job"]["id"] == str(job_id)
    assert db.get(IngestionJob, job_id).retry_progress is None
    assert db.get(CreditAccount, workspace_id).balance == balance - (Decimal("0.2100") if outcome == "recover" else 0)
    keys = list(db.scalars(select(CreditLedgerEntry.idempotency_key).where(
        CreditLedgerEntry.idempotency_key.like(f"usage:{job_id}:%"))))
    expected_keys = [f"usage:{job_id}:{suffix}" for suffix in ("hold", "release")]
    if outcome == "recover":
        expected_keys.append(f"usage:{job_id}:embedding:charge")
    assert sorted(keys) == sorted(expected_keys)
    assert len(list(db.scalars(select(UsageEvent).where(UsageEvent.request_id == job_id)))) == (outcome == "recover")
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)).all()
    assert [c.embedding[0] if c.embedding is not None else None for c in chunks] == (
        list(range(21)) if outcome == "recover" else [None] * 21)
    assert all(c.embedding_status == (EmbeddingStatus.READY if outcome == "recover" else EmbeddingStatus.FAILED)
               for c in chunks)
    if outcome in ("exhaust", "cancel"):
        manual = owner.post(f"/jobs/{job_id}/retry")
        assert manual.status_code == 202
        assert dispatched == [(str(doc_id), manual.json()["id"])]
        assert not manual.json()["retry_progress"]
        manual_retry = True
        tasks.embed_document.run(*dispatched[0])
        assert calls[-2:] == ["0", "20"]
        assert owner.get(f"/uploads/{upload_id}").json()["document_status"] == "ready"
        db.expire_all()
        assert db.get(CreditAccount, workspace_id).balance == balance - Decimal("0.2100")
    elif outcome == "delete":
        assert owner.post(f"/jobs/{job_id}/retry").status_code == 409


def test_encrypted_pdf_upload_keeps_object_bytes_hash_and_version_history(direct):
    from pdf_fixtures import make_pdf

    owner, outsider, db, _, kb, storage, _, processed = direct
    versions = []
    for content in (make_pdf(), make_pdf(password="")):
        previous = versions[-1][0] if versions else None
        upload = create(owner, kb, content, "evidence.pdf", **({"version_of": str(previous.id)} if previous else {}))
        row = send(owner, db, storage, upload, content)
        assert owner.post(f"/uploads/{row.id}/complete").status_code == 202
        uploads.finalize_upload(str(row.id))
        db.expire_all()
        document = db.get(Document, db.get(DocumentUpload, row.id).document_id)
        assert document.content_hash == hashlib.sha256(content).hexdigest()
        assert document.size_bytes == len(content)
        assert storage.objects[document.object_key] == content
        assert processed[-1][1]["prepared_content"] == content
        assert document.version_number == len(versions) + 1
        if previous:
            assert document.supersedes_id == previous.id
            assert document.version_group_id == previous.version_group_id
            assert not document.is_active  # Existing version remains active until indexing succeeds.
        assert outsider.get(f"/uploads/{row.id}").status_code == 404
        versions.append((document, content))
    assert versions[0][0].object_key != versions[1][0].object_key
    assert all(storage.objects[document.object_key] == content for document, content in versions)


def test_scope_size_parts_fingerprint_and_expiry(direct):
    owner, outsider, db, _, kb, storage, _, _ = direct
    assert outsider.post(f"/knowledge-bases/{kb}/uploads", json={"filename": "x.txt", "size_bytes": 1}).status_code == 404
    for invalid in (0, 200 * 1024 * 1024 + 1, True):
        assert owner.post(f"/knowledge-bases/{kb}/uploads", json={"filename": "x.txt", "size_bytes": invalid}).status_code == 422
    maximum = create(owner, kb, b"x", size_bytes=200 * 1024 * 1024)
    assert maximum["size_bytes"] == 200 * 1024 * 1024
    for name in ("../x.txt", "x.exe", "a\n.txt"):
        assert owner.post(f"/knowledge-bases/{kb}/uploads", json={"filename": name, "size_bytes": 1}).status_code == 415
    upload = create(owner, kb)
    identifier = upload["id"]
    for action in ("", "/parts/1", "/complete"):
        response = outsider.get(f"/uploads/{identifier}") if not action else outsider.post(
            f"/uploads/{identifier}{action}", json={"sha256": "a"*64} if "parts" in action else {})
        assert response.status_code == 404
    assert owner.post(f"/uploads/{identifier}/parts/2", json={"sha256": "a"*64}).status_code == 422
    assert owner.post(f"/uploads/{identifier}/complete").status_code == 409
    assert owner.post(f"/uploads/{identifier}/parts/1", json={"sha256": "a"*64}).status_code == 200
    assert owner.post(f"/uploads/{identifier}/parts/1", json={"sha256": "a"*64}).status_code == 200
    assert owner.post(f"/uploads/{identifier}/parts/1", json={"sha256": "b"*64}).status_code == 409
    row = db.get(DocumentUpload, UUID(identifier))
    row.expires_at = auth.now() - timedelta(seconds=1)
    db.commit()
    assert owner.get(f"/uploads/{identifier}").status_code == 410
    uploads.cleanup_uploads()
    db.expire_all()
    assert db.get(DocumentUpload, row.id).state == "cleaned"


@pytest.mark.parametrize("failure", ["hash", "format", "suspended", "permission", "size"])
def test_worker_rejects_bad_content_or_revoked_permissions_before_parsing(direct, failure):
    owner, _, db, identity, kb, storage, _, processed = direct
    content = b"not a pdf" if failure == "format" else b"original"
    upload = create(owner, kb, content, "document.pdf" if failure == "format" else "document.txt")
    row = send(owner, db, storage, upload, content)
    assert owner.post(f"/uploads/{row.id}/complete").status_code == 202
    if failure == "hash":
        storage.objects[row.object_key] = b"modified"
    elif failure == "size":
        storage.objects[row.object_key] = content + b"more"
    elif failure == "suspended":
        db.get(User, UUID(identity["user"]["id"])).is_suspended = True
    elif failure == "permission":
        db.get(Membership, (UUID(identity["workspace_id"]), UUID(identity["user"]["id"]))).role = "viewer"
    db.commit()
    uploads.finalize_upload(str(row.id))
    db.expire_all()
    assert db.get(DocumentUpload, row.id).state == "failed"
    assert processed == []
    assert db.scalar(select(func.count()).select_from(Document)) == 0
    assert row.object_key not in storage.objects


def test_duplicate_versions_cancel_and_reader_permissions(direct):
    owner, other, db, identity, kb, storage, _, processed = direct
    first = create(owner, kb)
    first_row = send(owner, db, storage, first, b"document")
    owner.post(f"/uploads/{first_row.id}/complete")
    uploads.finalize_upload(str(first_row.id))
    db.expire_all()
    first_id = db.get(DocumentUpload, first_row.id).document_id
    duplicate = create(owner, kb)
    row = send(owner, db, storage, duplicate, b"document")
    owner.post(f"/uploads/{row.id}/complete")
    uploads.finalize_upload(str(row.id))
    db.expire_all()
    assert db.get(DocumentUpload, row.id).state == "failed"
    assert db.scalar(select(func.count()).select_from(Document)) == 1
    version = create(owner, kb, b"new version", version_of=str(first_id))
    row = send(owner, db, storage, version, b"new version")
    owner.post(f"/uploads/{row.id}/complete")
    uploads.finalize_upload(str(row.id))
    db.expire_all()
    updated = db.get(Document, db.get(DocumentUpload, row.id).document_id)
    assert updated.version_number == 2 and updated.supersedes_id == first_id and not updated.is_active
    cancelled = create(owner, kb)
    assert owner.delete(f"/uploads/{cancelled['id']}").status_code == 204
    assert owner.delete(f"/uploads/{cancelled['id']}").status_code == 204
    assert owner.post(f"/uploads/{cancelled['id']}/complete").status_code == 409
    share(owner, "outsider@example.com", "viewer")
    switch(other, identity["workspace_id"])
    assert other.post(f"/knowledge-bases/{kb}/uploads", json={"filename": "x.txt", "size_bytes": 1}).status_code == 403


def test_storage_admin_encryption_version_test_and_secret_redaction(accounts, monkeypatch):
    (owner, _, _), db = accounts
    identity = register(owner)
    assert owner.get("/site/admin/storage").status_code == 403
    db.get(User, UUID(identity["user"]["id"])).is_site_admin = True
    db.commit()
    settings = site_auth.get_settings().model_copy(update={"site_config_encryption_key": SecretStr(Fernet.generate_key().decode())})
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    body = {"version": 0, "bucket": "zk-1309923787", "region": "ap-guangzhou",
        "secret_id": "COS-TEST-IDENTIFIER", "secret_key": "COS-TEST-SECRET"}
    response = owner.put("/site/admin/storage", json=body)
    assert response.status_code == 200, response.text
    assert "COS-TEST-SECRET" not in response.text and "COS-TEST-IDENTIFIER" not in response.text
    row = db.get(SiteStorageSettings, 1)
    assert row.encrypted_secret_key != body["secret_key"]
    assert site_auth.reveal(row.encrypted_secret_key) == body["secret_key"]
    assert owner.put("/site/admin/storage", json=body).status_code == 409
    fake = FakeCos()
    monkeypatch.setattr(storage_settings, "CosStorage", lambda _: fake)
    tested = owner.post("/site/admin/storage/test", json={"version": 1})
    assert tested.status_code == 200 and tested.json()["success"] and not tested.json()["active"]
    assert fake.objects == fake.uploads == {}
    row.active = True
    db.commit()
    assert owner.put("/site/admin/storage", json={**body, "version": 1, "region": "ap-shanghai"}).status_code == 409
    assert owner.put("/site/admin/storage", json={**body, "version": 1, "clear_credentials": True}).status_code == 409
    saved = owner.put("/site/admin/storage", json={**body, "version": 1, "secret_id": "", "secret_key": ""})
    assert saved.status_code == 200 and saved.json()["has_secret_key"]
    monkeypatch.setattr(storage_settings, "check_connection", lambda _: False)
    failed_rotation = owner.put("/site/admin/storage", json={**body, "version": 2, "secret_key": "invalid-new-secret"})
    assert failed_rotation.status_code == 503
    db.expire_all()
    assert site_auth.reveal(db.get(SiteStorageSettings, 1).encrypted_secret_key) == body["secret_key"]
    audit = str([event.details for event in db.scalars(select(SiteAuditEvent))])
    assert body["secret_id"] not in audit and body["secret_key"] not in audit


def test_sdk_part_signature_binds_path_query_and_exact_size():
    from types import SimpleNamespace
    storage = CosStorage.__new__(CosStorage)
    storage.bucket = "test-123"
    calls = []
    storage.sdk = SimpleNamespace(get_presigned_url=lambda **kwargs: calls.append(kwargs) or "https://signed.invalid")
    assert storage.part_url("scope/file.txt", "upload", 3, 100) == "https://signed.invalid"
    assert calls == [{"Bucket": "test-123", "Key": "scope/file.txt", "Method": "PUT", "Expired": 900,
        "Params": {"uploadId": "upload", "partNumber": "3"}, "Headers": {"Content-Length": "100"}}]


def test_real_sdk_signature_includes_part_number_upload_id_and_content_length():
    from qcloud_cos import CosConfig, CosS3Client
    from urllib.parse import parse_qs, urlparse
    storage = CosStorage.__new__(CosStorage)
    storage.bucket = "zk-1309923787"
    storage.sdk = CosS3Client(CosConfig(Region="ap-guangzhou", SecretId="test-id", SecretKey="test-key", SignParams=True))
    signed = parse_qs(urlparse(storage.part_url("library/test.txt", "upload-id", 2, 123)).query)
    assert "content-length" in signed["q-header-list"][0]
    assert set(signed["q-url-param-list"][0].split(";")) == {"partnumber", "uploadid"}
    assert signed["partNumber"] == ["2"] and signed["uploadId"] == ["upload-id"]
    altered = parse_qs(urlparse(storage.part_url("library/test.txt", "upload-id", 2, 124)).query)
    assert altered["q-signature"] != signed["q-signature"]


@pytest.mark.parametrize("size", [0, 2 * 1024 * 1024 + 13], ids=["empty", "multiple-chunks"])
def test_download_reads_real_sdk_stream_body_and_closes_connection(size):
    from types import SimpleNamespace
    from requests import Response
    from qcloud_cos.streambody import StreamBody

    content = b"x" * size
    response = Response()
    response.status_code = 200
    response.headers["Content-Length"] = str(len(content))
    response.raw = io.BytesIO(content)
    storage = CosStorage.__new__(CosStorage)
    storage.bucket = "test-123"
    storage.sdk = SimpleNamespace(get_object=lambda **_: {"Body": StreamBody(response)})
    with storage.download("document.txt") as downloaded:
        assert downloaded.tell() == 0
        assert downloaded.read() == content
    assert response.raw.closed


def test_download_failure_closes_files_and_hides_provider_details(monkeypatch):
    from types import SimpleNamespace
    from app import cos_storage
    from requests import Response
    from qcloud_cos.streambody import StreamBody

    class BrokenStream(io.BytesIO):
        def read(self, *_args, **_kwargs):
            raise OSError("private signed URL and provider details")

    response = Response()
    response.headers["Content-Length"] = "10"
    response.raw = BrokenStream()
    temporary = io.BytesIO()
    monkeypatch.setattr(cos_storage, "TemporaryFile", lambda: temporary)
    storage = CosStorage.__new__(CosStorage)
    storage.bucket = "test-123"
    storage.sdk = SimpleNamespace(get_object=lambda **_: {"Body": StreamBody(response)})
    with pytest.raises(HTTPException) as caught:
        storage.download("document.txt")
    assert caught.value.status_code == 503
    assert "private" not in caught.value.detail
    assert response.raw.closed and temporary.closed


def test_orphan_cleanup_never_deletes_original_versions_or_active_uploads(direct, monkeypatch):
    owner, _, db, _, kb, storage, _, _ = direct
    from test_retrieval import add_document
    from app.models import DocumentStatus, KnowledgeBase
    document = add_document(db, db.get(KnowledgeBase, kb), "retained evidence")
    document.object_key = f"{kb}/{uuid4()}.txt"
    document.status = DocumentStatus.DELETED
    db.add(SiteStorageSettings(id=1, bucket="zk-1309923787", region="ap-guangzhou", active=True))
    db.commit()
    active = create(owner, kb)
    active_key = db.get(DocumentUpload, UUID(active["id"])).object_key
    orphan = f"{kb}/{uuid4()}.txt"
    other = "unmanaged-file.txt"
    recent = f"{kb}/{uuid4()}.txt"
    for key in (document.object_key, active_key, orphan, other, recent, "_ocr/abandoned.pdf"):
        storage.objects[key] = b"retained"
    old = (auth.now() - timedelta(days=2)).isoformat()
    monkeypatch.setattr(storage, "iter_objects", lambda: [
        {"Key": key, "LastModified": auth.now().isoformat() if key == recent else old} for key in storage.objects.copy()])
    uploads.cleanup_orphans()
    assert set(storage.objects) == {document.object_key, active_key, other, recent}


def test_completion_recovers_sealed_object_after_lost_response(direct):
    owner, _, db, _, kb, storage, _, processed = direct
    upload = create(owner, kb, b"sealed")
    row = send(owner, db, storage, upload, b"sealed")
    row.state = "completing"
    storage.complete(row.object_key, row.multipart_id, [{"PartNumber": 1, "ETag": '"part-1"'}])
    db.commit()
    response = owner.post(f"/uploads/{row.id}/complete")
    assert response.status_code == 202 and response.json()["state"] == "verifying"
    assert storage.seals == 1
    uploads.finalize_upload(str(row.id))
    assert len(processed) == 1
