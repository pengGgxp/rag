"""Storage contract and cutover tests; never access cloud services."""
import hashlib
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import alibabacloud_oss_v2 as oss
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import tasks
from app.operations import migrate_storage
from app.infra import storage_backend
from app.files import settings as storage_settings

from app.identity import site_auth
from app.knowledge.ingestion import pipeline
from app.models import (
    ChatAttachment, DocumentStatus, KnowledgeBase, SiteAuditEvent, SiteStorageSettings,
    StorageMigration, StorageMigrationObject, User, WorkspaceFile, WorkspaceFileVersion,
)
from app.infra.oss_storage import OssStorage
from app.files.settings import StorageInput
from test_auth import library, register
from test_cos_uploads import FakeCos
from test_retrieval import add_document


@pytest.fixture
def admin(accounts, monkeypatch):
    (owner, other, _), db = accounts
    identity = register(owner)
    db.get(User, UUID(identity["user"]["id"])).is_site_admin = True
    db.commit()
    config = site_auth.get_settings().model_copy(update={
        "site_config_encryption_key": SecretStr(Fernet.generate_key().decode())})
    monkeypatch.setattr(site_auth, "get_settings", lambda: config)
    return owner, other, db, identity


def test_independent_provider_settings_permissions_secrets_and_no_auto_activation(admin, monkeypatch):
    owner, other, db, _ = admin
    register(other, "other@example.com")
    assert other.get("/site/admin/storage").status_code == 403
    base = {"provider": "oss", "version": 0, "bucket": "zhiku-test", "region": "cn-hangzhou",
        "secret_id": "dedicated-access-id", "secret_key": "dedicated-access-secret"}
    assert other.put("/site/admin/storage", json=base).status_code == 403
    response = owner.put("/site/admin/storage", json=base)
    assert response.status_code == 200, response.text
    assert base["secret_id"] not in response.text and base["secret_key"] not in response.text
    row = db.get(SiteStorageSettings, 2)
    assert row.provider == "oss" and site_auth.reveal(row.encrypted_secret_key) == base["secret_key"]
    cos = owner.put("/site/admin/storage", json={**base, "provider": "cos", "bucket": "zhiku-123",
        "region": "ap-guangzhou"})
    assert cos.status_code == 200
    fake = FakeCos()
    monkeypatch.setattr(storage_settings, "OssStorage", lambda _: fake)
    tested = owner.post("/site/admin/storage/test", json={"provider": "oss", "version": 1})
    assert tested.json()["success"] and not tested.json()["active"]
    assert not fake.objects and not fake.uploads
    listing = owner.get("/site/admin/storage").json()
    assert listing["active_provider"] is None and len(listing["connections"]) == 2
    assert owner.put("/site/admin/storage", json=base).status_code == 409
    kept = owner.put("/site/admin/storage", json={**base, "version": 1, "secret_id": "", "secret_key": ""})
    assert kept.status_code == 200 and kept.json()["has_secret_key"]
    cleared = owner.put("/site/admin/storage", json={**base, "version": 2, "clear_credentials": True})
    assert cleared.status_code == 200 and not cleared.json()["has_secret_key"]
    audit = str([event.details for event in db.scalars(select(SiteAuditEvent))])
    assert base["secret_key"] not in audit and base["secret_id"] not in audit


def test_active_oss_rotation_failure_preserves_original_and_position(admin, monkeypatch):
    owner, _, db, _ = admin
    body = {"provider": "oss", "version": 0, "bucket": "zhiku-test", "region": "cn-hangzhou",
        "secret_id": "old-id", "secret_key": "old-key"}
    assert owner.put("/site/admin/storage", json=body).status_code == 200
    db.get(SiteStorageSettings, 2).active = True
    db.commit()
    assert owner.put("/site/admin/storage", json={**body, "version": 1, "bucket": "other-bucket"}).status_code == 409
    assert owner.put("/site/admin/storage", json={**body, "version": 1, "clear_credentials": True}).status_code == 409
    monkeypatch.setattr(storage_settings, "check_connection", lambda _: False)
    assert owner.put("/site/admin/storage", json={**body, "version": 1, "secret_key": "new-key"}).status_code == 503
    db.expire_all()
    row = db.get(SiteStorageSettings, 2)
    assert row.version == 1 and site_auth.reveal(row.encrypted_secret_key) == "old-key"
    assert owner.get("/site/admin/storage").json()["active_provider"] == "oss"


@pytest.mark.parametrize("changes", [
    {"bucket": "bad_bucket"}, {"bucket": "a"}, {"bucket": "-bucket"},
    {"region": "https://malicious.invalid"}, {"region": "oss-cn-hangzhou"},
    {"provider": "other"}, {"endpoint": "http://localhost"},
])
def test_invalid_oss_locations(changes):
    with pytest.raises(ValidationError):
        StorageInput(**({"provider": "oss", "version": 0, "bucket": "test-bucket", "region": "cn-hangzhou"} | changes))


def test_oss_missing_encryption_key_is_not_persisted(admin, monkeypatch):
    owner, _, db, _ = admin
    settings = site_auth.get_settings().model_copy(update={"site_config_encryption_key": SecretStr("")})
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    result = owner.put("/site/admin/storage", json={"provider": "oss", "version": 0,
        "bucket": "test-bucket", "region": "cn-hangzhou", "secret_key": "private-key"})
    assert result.status_code == 503 and "private-key" not in result.text
    db.rollback()
    assert db.get(SiteStorageSettings, 2) is None


@pytest.fixture
def stores(accounts):
    (owner, _, _), db = accounts
    identity = register(owner)
    kb = library(owner)
    content = b"retained historical document"
    doc = add_document(db, db.get(KnowledgeBase, kb), "evidence")
    doc.size_bytes, doc.content_hash = len(content), hashlib.sha256(content).hexdigest()
    doc.status, doc.is_active = DocumentStatus.DELETED, False
    db.add_all([
        SiteStorageSettings(id=1, provider="cos", active=True, version=3, tested_version=3,
            bucket="source-123", region="ap-guangzhou"),
        SiteStorageSettings(id=2, provider="oss", active=False, version=2, tested_version=2,
            bucket="destination", region="cn-hangzhou"),
    ])
    user_id, account_id = UUID(identity["user"]["id"]), UUID(identity["user"]["id"])
    file = WorkspaceFile(account_id=account_id, path="file.txt", content_type="text/plain",
        size_bytes=4, content_hash=hashlib.sha256(b"next").hexdigest(), current_version=2,
        created_by=user_id, updated_by=user_id)
    db.add(file)
    db.flush()
    for number, body in ((1, b"prev"), (2, b"next")):
        db.add(WorkspaceFileVersion(file_id=file.id, account_id=account_id, version=number,
            object_key=f"_resources/{file.id}/{number}", content_type="text/plain", size_bytes=len(body),
            content_hash=hashlib.sha256(body).hexdigest(), created_by=user_id))
    db.add(ChatAttachment(account_id=account_id, user_id=user_id, filename="original.txt",
        content_type="text/plain", size_bytes=len(content), content_hash=doc.content_hash,
        object_key=doc.object_key, kind="text", deleted_at=datetime.now(timezone.utc)))
    db.commit()
    source, target = FakeCos(), FakeCos()
    source.objects = {doc.object_key: content, f"_resources/{file.id}/1": b"prev", f"_resources/{file.id}/2": b"next"}
    return db, source, target, lambda row: source if row.provider == "cos" else target


def test_manifest_dedup_history_cutover_idempotency_and_reverse_migration(stores):
    db, source, target, factory = stores
    before = migrate_storage.manifest(db)
    preview = migrate_storage.migrate(db, target_provider="oss", version=2, factory=factory)
    assert preview["objects"] == 3 and not target.objects
    with pytest.raises(ValueError, match="停止"):
        migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, factory=factory)
    result = migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    assert result["verified"] and result["changed"]
    assert target.objects == source.objects
    assert migrate_storage.manifest(db) == before
    assert db.get(SiteStorageSettings, 2).active and not db.get(SiteStorageSettings, 1).active
    assert migrate_storage.migrate(db, target_provider="oss", version=3, execute=True, factory=factory)["changed"] is False
    # Same verified path supports a rollback without losing current objects.
    reverse = migrate_storage.migrate(db, target_provider="cos", version=4, execute=True, offline=True, factory=factory)
    assert reverse["changed"] and db.get(SiteStorageSettings, 1).active


def test_missing_source_and_resumable_copy_never_switch_early(stores):
    db, source, target, factory = stores
    key, body = next(iter(source.objects.items()))
    del source.objects[key]
    with pytest.raises(ValueError, match="resume"):
        migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    assert db.get(SiteStorageSettings, 1).active and not db.get(SiteStorageSettings, 2).active
    run = db.scalar(select(StorageMigration))
    assert run.status == "failed"
    source.objects[key] = body
    result = migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True,
        resume=run.id, factory=factory)
    assert result["verified"] and db.scalar(select(StorageMigration)).status == "completed"


def test_target_conflict_not_overwritten_and_config_version_change_blocks_resume(stores):
    db, source, target, factory = stores
    key = next(iter(source.objects))
    target.objects[key] = b"do not overwrite"
    with pytest.raises(ValueError, match="resume"):
        migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    assert target.objects[key] == b"do not overwrite" and db.get(SiteStorageSettings, 1).active
    run = db.scalar(select(StorageMigration))
    db.get(SiteStorageSettings, 2).version = 3
    db.get(SiteStorageSettings, 2).tested_version = 3
    db.commit()
    with pytest.raises(ValueError, match="配置不符"):
        migrate_storage.migrate(db, target_provider="oss", version=3, execute=True, offline=True, resume=run.id, factory=factory)


def test_only_one_active_store_and_ocr_uses_oss(stores, monkeypatch):
    db, source, target, factory = stores
    db.get(SiteStorageSettings, 2).active = True
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    monkeypatch.setattr(storage_backend, "configured_backend", factory)
    assert pipeline.ocr_storage(db) is source
    migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    assert pipeline.ocr_storage(db) is target


def bare_oss(**methods):
    adapter = OssStorage.__new__(OssStorage)
    adapter.bucket, adapter.region = "test-bucket", "cn-hangzhou"
    adapter.sdk = SimpleNamespace(**methods)
    adapter.credentials = oss.credentials.StaticCredentialsProvider("synthetic-id", "synthetic-secret")
    return adapter


def test_sdk_signatures_bind_key_part_size_and_expiry():
    calls = []
    adapter = bare_oss(presign=lambda request, **kwargs: calls.append((request, kwargs)) or SimpleNamespace(url="https://private.invalid"))
    assert adapter.part_url("private/file", "upload-1", 2, 123).startswith("https://private.invalid")
    request, options = calls[0]
    assert isinstance(request, oss.UploadPartRequest)
    assert (request.bucket, request.key, request.upload_id, request.part_number, request.content_length) == (
        "test-bucket", "private/file", "upload-1", 2, 123)
    assert options["expires"].total_seconds() == 900
    adapter.read_url("private/file")
    assert isinstance(calls[1][0], oss.GetObjectRequest) and calls[1][1]["expires"].total_seconds() == 3600
    adapter.read_url("private/file", seconds=90, inline=True)
    assert calls[2][0].response_content_disposition == "inline"
    assert calls[2][1]["expires"].total_seconds() == 90


def test_sdk_real_presigning_has_no_network_and_signed_upload_parameters(admin):
    _, _, db, _ = admin
    row = SiteStorageSettings(provider="oss", bucket="test-bucket", region="cn-hangzhou",
        encrypted_secret_id=site_auth.protect("test-access"), encrypted_secret_key=site_auth.protect("test-secret"))
    adapter = OssStorage(row)
    url = adapter.part_url("files/chinese.txt", "upload-1", 3, 1024)
    assert url.startswith("https://test-bucket.oss-cn-hangzhou.aliyuncs.com/")
    assert "uploadId=upload-1" in url and "partNumber=3" in url and "x-oss-signature=" in url
    assert "x-oss-additional-headers=content-length" in url
    assert "test-secret" not in url


def test_sdk_range_and_download_body_closed():
    streams = []
    requests = []
    def get(request):
        requests.append(request)
        class SdkBody(io.BytesIO):
            def read(self):
                return super().read()

            def iter_bytes(self, chunk_size):
                while chunk := super().read(chunk_size):
                    yield chunk
        body = SdkBody(b"hello")
        streams.append(body)
        return SimpleNamespace(body=body)
    adapter = bare_oss(get_object=get)
    with adapter.download("file") as stream:
        assert stream.read() == b"hello"
    assert streams[0].closed
    with adapter.download_range("file", 1, 3) as stream:
        assert stream.read() == b"hello"
    assert requests[-1].range_header == "bytes=1-3" and streams[-1].closed


def test_sdk_pagination_and_multipart_normalization():
    calls = []
    def listing(request):
        calls.append(request)
        return SimpleNamespace(contents=[SimpleNamespace(key="a", size=4, last_modified=None, etag="etag-a")],
            is_truncated=len(calls) == 1, next_continuation_token="page2")
    adapter = bare_oss(list_objects_v2=listing)
    assert len(list(adapter.iter_objects())) == 2 and calls[1].continuation_token == "page2"
    part_calls = []
    def parts(request):
        part_calls.append(request)
        return SimpleNamespace(parts=[SimpleNamespace(part_number=len(part_calls), size=8, etag="etag")],
            is_truncated=len(part_calls) == 1, next_part_number_marker=1)
    adapter.sdk.list_parts = parts
    assert adapter.parts("file", "u") == [
        {"PartNumber": 1, "Size": 8, "ETag": "etag"}, {"PartNumber": 2, "Size": 8, "ETag": "etag"}]
    assert part_calls[1].part_number_marker == 1
    completions = []
    adapter.sdk.complete_multipart_upload = lambda request: completions.append(request)
    adapter.complete("file", "u", [{"PartNumber": 1, "ETag": "etag"}])
    assert completions[0].complete_multipart_upload.parts[0].etag == "etag"


def test_vendor_errors_are_redacted_without_fallback():
    def fail(_):
        raise RuntimeError("PRIVATE-CREDENTIAL signed-url")
    adapter = bare_oss(head_object=fail)
    with pytest.raises(HTTPException) as error:
        adapter.head("file")
    assert "PRIVATE" not in error.value.detail and "signed-url" not in error.value.detail


def test_sdk_missing_object_is_normalized_but_permission_errors_are_not():
    def missing(_):
        service = oss.exceptions.ServiceError(status_code=404, code="NoSuchKey", message="private detail",
            request_id="test", ec="", timestamp="", request_target="test")
        raise oss.exceptions.OperationError(name="HeadObject", error=service)
    adapter = bare_oss(head_object=missing, abort_multipart_upload=missing)
    assert adapter.head("missing") is None
    assert adapter.abort("file", "missing-upload") is None
    def forbidden(_):
        service = oss.exceptions.ServiceError(status_code=403, code="AccessDenied", message="private detail",
            request_id="test", ec="", timestamp="", request_target="test")
        raise oss.exceptions.OperationError(name="HeadObject", error=service)
    adapter.sdk.head_object = forbidden
    with pytest.raises(HTTPException) as error:
        adapter.head("file")
    assert error.value.status_code == 503 and "private detail" not in error.value.detail


def test_queued_ingestion_blocks_cutover(stores):
    from app.models import IngestionJob, JobState, Document
    db, source, target, factory = stores
    document = db.scalar(select(Document))
    db.add(IngestionJob(document_id=document.id, state=JobState.QUEUED))
    db.commit()
    with pytest.raises(ValueError, match="排空"):
        migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    assert not target.objects and db.get(SiteStorageSettings, 1).active


def test_object_save_failure_resumes_without_recopying_verified_objects(stores):
    db, source, target, factory = stores
    put = target.put
    counts = {}
    failed_key = sorted(source.objects)[1]
    def fail_once(key, stream, content_type):
        counts[key] = counts.get(key, 0) + 1
        if key == failed_key and counts[key] == 1:
            raise RuntimeError("synthetic network interruption")
        return put(key, stream, content_type)
    target.put = fail_once
    with pytest.raises(ValueError, match="resume"):
        migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    run = db.scalar(select(StorageMigration))
    assert db.scalar(select(StorageMigrationObject).where(StorageMigrationObject.verified.is_(True)))
    migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, resume=run.id, factory=factory)
    assert counts[sorted(source.objects)[0]] == 1 and counts[failed_key] == 2
    assert target.objects == source.objects


def test_reverse_migration_preserves_files_added_after_cutover(stores):
    from app.models import Document
    db, source, target, factory = stores
    migrate_storage.migrate(db, target_provider="oss", version=2, execute=True, offline=True, factory=factory)
    document = db.scalar(select(Document))
    body = b"created after switching to OSS"
    db.add(Document(knowledge_base_id=document.knowledge_base_id, filename="new.txt", content_type="text/plain",
        size_bytes=len(body), object_key="new-after-cutover", content_hash=hashlib.sha256(body).hexdigest()))
    db.commit()
    target.objects["new-after-cutover"] = body
    migrate_storage.migrate(db, target_provider="cos", version=4, execute=True, offline=True, factory=factory)
    assert source.objects["new-after-cutover"] == body and db.get(SiteStorageSettings, 1).active
