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
from app.models import Document, DocumentUpload, IngestionJob, Membership, SiteAuditEvent, SiteStorageSettings, User
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


@pytest.fixture
def direct(accounts, monkeypatch):
    (owner, outsider, _), db = accounts
    identity = register(owner)
    register(outsider, "outsider@example.com")
    kb = library(owner)
    storage = FakeCos()
    monkeypatch.setattr(uploads, "current_cos", lambda *_: storage)
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
    assert tested.status_code == 200 and tested.json()["success"] and tested.json()["active"]
    assert fake.objects == fake.uploads == {}
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
