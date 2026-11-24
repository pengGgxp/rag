import asyncio
import io
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.files import quotas as billing
from app.identity.auth import Access, now
from app.models import AgentSkill, UserCreditAccount, CreditLedgerEntry, WorkspaceFile, WorkspaceFileVersion
from app.files.downloads import MeteredResponse, byte_range
from app.storage_models import StorageEntitlement, StorageQuotaSettings, StorageTransfer
from test_site_admin import setup


def configure(client, **patch):
    body = client.get("/site/admin/storage/quotas").json()
    body.update(patch)
    response = client.put("/site/admin/storage/quotas", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def actor(a):
    return Access(UUID(a["user"]["id"]), "")


def test_config_permissions_validation_and_conflicts(accounts):
    admin, member, anonymous, db, a, b = setup(accounts)
    assert member.get("/site/admin/storage/quotas").status_code == 403
    assert anonymous.get("/storage/allowances").status_code == 401
    config = admin.get("/site/admin/storage/quotas").json()
    assert config["free_storage_bytes"] == 512*1024**2
    assert all(p["price_credits"] is None and not p["enabled"] for p in config["packages"])
    config["packages"][0]["enabled"] = True
    assert admin.put("/site/admin/storage/quotas", json=config).status_code == 422
    config["packages"][0]["price_credits"] = 0
    assert admin.put("/site/admin/storage/quotas", json=config).status_code == 200
    assert admin.put("/site/admin/storage/quotas", json=config).status_code == 409


def test_purchase_atomic_idempotent_snapshot_no_multiplier(accounts):
    admin, member, _, db, a, b = setup(accounts)
    config = admin.get("/site/admin/storage/quotas").json()
    config["packages"][0].update(enabled=True, price_credits="3.1234")
    config = configure(admin, packages=config["packages"])
    access = actor(a)
    account = db.get(UserCreditAccount, access.user_id)
    original = account.balance
    body = {"package_id": "storage-5", "version": config["version"], "request_id": str(uuid4())}
    first = admin.post("/storage/purchases", json=body)
    assert first.status_code == 201, first.text
    assert admin.post("/storage/purchases", json=body).json()["id"] == first.json()["id"]
    db.refresh(account)
    assert account.balance == original-Decimal("3.1234")
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
        CreditLedgerEntry.kind == "storage_package_purchase")) == 1
    config["packages"][0].update(price_credits=999999, enabled=False)
    configure(admin, packages=config["packages"])
    assert admin.post("/storage/purchases", json=body).json()["price_credits"] == "3.1234"
    other = {**body, "request_id": str(uuid4())}
    assert admin.post("/storage/purchases", json=other).status_code == 409
    assert len(admin.get("/storage/allowances").json()["history"]) == 1
    assert not member.get("/storage/allowances").json()["history"]


def test_failed_debit_rolls_back_entitlement(accounts):
    admin, _, _, db, a, b = setup(accounts)
    config = admin.get("/site/admin/storage/quotas").json()
    config["packages"][0].update(enabled=True, price_credits=2000000)
    config = configure(admin, packages=config["packages"])
    response = admin.post("/storage/purchases", json={"package_id": "storage-5", "version": config["version"], "request_id": str(uuid4())})
    assert response.status_code == 402
    # Request dependency rollback is normally performed on session close.
    db.rollback()
    assert not db.scalar(select(StorageEntitlement).where(StorageEntitlement.kind == "purchase"))


def test_only_site_admin_can_grant_storage_to_another_account(accounts):
    admin, member, _, db, a, b = setup(accounts)
    body = {"storage_bytes": 100, "download_bytes": 50, "days": 30,
            "reason": "Verified adjustment", "request_id": str(uuid4())}
    path = f"/site/admin/users/{b['user']['id']}/storage/grants"
    assert member.post(path, json=body).status_code == 403
    first = admin.post(path, json=body)
    assert first.status_code == 201, first.text
    assert admin.post(path, json=body).json()["id"] == first.json()["id"]
    assert admin.post(path, json={**body, "storage_bytes": 101}).status_code == 409
    assert member.get("/storage/allowances").json()["limit_bytes"] == 512*1024**2+100
    assert admin.get("/storage/allowances").json()["limit_bytes"] == 512*1024**2


def test_transfer_reserve_checkpoint_cancel_and_crash_recovery(accounts):
    admin, _, _, db, a, b = setup(accounts)
    configure(admin, free_download_bytes=20)
    access = actor(a)
    first = billing.reserve_download(db, access, 15, "test")
    db.commit()
    with pytest.raises(HTTPException) as error:
        billing.reserve_download(db, access, 6, "test")
    assert error.value.status_code == 402
    db.rollback()
    billing.transfer_checkpoint(db, first, preparing=5)
    billing.transfer_checkpoint(db, first, sent=5)
    billing.transfer_checkpoint(db, first, preparing=3)
    db.commit()
    row = db.get(StorageTransfer, first)
    row.lease_until = now()-timedelta(seconds=1)
    db.commit()
    billing.recover_transfers(db, access.user_id)
    db.commit()
    db.refresh(row)
    assert (row.sent_bytes, row.uncertain_bytes, row.status) == (5, 3, "interrupted")
    grant = db.scalar(select(StorageEntitlement).where(StorageEntitlement.kind == "free"))
    assert (grant.used_bytes, grant.reserved_bytes) == (5, 0)
    assert billing.reserve_download(db, access, 15, "resume")


def test_earliest_expiry_allocation_and_month_reset(accounts):
    admin, _, _, db, a, b = setup(accounts)
    configure(admin, free_download_bytes=10)
    access = actor(a)
    extra = StorageEntitlement(account_id=access.user_id, request_key="test", kind="grant", name="short",
        storage_bytes=0, download_bytes=5, expires_at=now()+timedelta(minutes=1))
    db.add(extra); db.commit()
    identifier = billing.reserve_download(db, access, 8, "test")
    row = db.get(StorageTransfer, identifier)
    assert row.allocations[0] == {"id": str(extra.id), "reserved": 5}
    billing.transfer_checkpoint(db, identifier, preparing=8)
    billing.transfer_checkpoint(db, identifier, sent=8, finish="completed")
    db.commit(); db.refresh(extra)
    assert extra.used_bytes == 5
    start, end = billing.month_bounds(now().replace(year=2026, month=12, day=31, hour=20))
    assert start.month == 1 and start.year == 2027 and end.month == 2


def test_free_allowance_changes_preserve_consumption_and_active_reservations(accounts):
    admin, _, _, db, a, _ = setup(accounts)
    access = actor(a)
    configure(admin, free_download_bytes=10)
    identifier = billing.reserve_download(db, access, 7, "quota-change")
    db.commit()
    billing.transfer_checkpoint(db, identifier, preparing=3)
    billing.transfer_checkpoint(db, identifier, sent=3)
    db.commit()
    configure(admin, free_download_bytes=2)
    assert admin.get("/storage/allowances").json()["download_remaining_bytes"] == 0
    db.expire_all()
    grant = billing.free_month(db, access.user_id)
    assert (grant.download_bytes, grant.used_bytes, grant.reserved_bytes) == (2, 3, 4)
    billing.transfer_checkpoint(db, identifier, finish="interrupted")
    db.commit()
    configure(admin, free_download_bytes=12)
    assert admin.get("/storage/allowances").json()["download_remaining_bytes"] == 9


def test_capacity_counts_only_current_file_and_never_expires_by_age(accounts):
    admin, _, _, db, a, b = setup(accounts)
    configure(admin, free_storage_bytes=10)
    access = actor(a)
    row = WorkspaceFile(account_id=access.user_id, path="test.txt", content_type="text/plain",
        size_bytes=2, content_hash="x", current_version=2, created_by=access.user_id,
        updated_by=access.user_id, expires_at=now()-timedelta(days=1))
    db.add(row); db.flush()
    for version in (1, 2):
        db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id,
            version=version, object_key=f"test-{version}", size_bytes=4 if version == 1 else 2, content_hash="x",
            content_type="text/plain", created_by=access.user_id))
    db.commit()
    assert billing.inventory(db, access.user_id)["used_bytes"] == 2
    with pytest.raises(HTTPException):
        billing.check_capacity(db, access.user_id, 9)
    old = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.version == 1))
    old.deleted_at = now(); db.commit()
    assert billing.check_capacity(db, access.user_id, 3)


def test_cleanup_rechecks_expiry_after_lock_and_releases_only_deleted_objects(accounts, monkeypatch):
    from app import tasks
    from app.infra import storage
    from sqlalchemy import update
    admin, _, _, db, a, _ = setup(accounts)
    access = actor(a)
    row = WorkspaceFile(account_id=access.user_id, path="output.txt", content_type="text/plain",
        size_bytes=4, content_hash="x", current_version=1, created_by=access.user_id,
        updated_by=access.user_id, expires_at=now()-timedelta(days=1))
    db.add(row); db.flush()
    db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id, version=1,
        object_key="cleanup-test", size_bytes=4, content_hash="x", content_type="text/plain", created_by=access.user_id))
    db.commit()
    monkeypatch.setattr(tasks, "SessionLocal", sessionmaker(bind=db.bind))
    deleted = []
    monkeypatch.setattr(storage, "delete_object", deleted.append)
    original_lock = billing.lock_workspace
    def renewed_while_waiting(transaction, identifier):
        original_lock(transaction, identifier)
        transaction.execute(update(WorkspaceFile).where(WorkspaceFile.id == row.id)
            .values(expires_at=now()+timedelta(days=3)))
    monkeypatch.setattr(billing, "lock_workspace", renewed_while_waiting)
    tasks.cleanup_workspace_files()
    assert not deleted
    monkeypatch.setattr(billing, "lock_workspace", original_lock)
    db.refresh(row)
    row.expires_at = now()-timedelta(days=1); row.deleted_at = now(); db.commit()
    def failed(_):
        raise IOError("temporary deletion failure")
    monkeypatch.setattr(storage, "delete_object", failed)
    tasks.cleanup_workspace_files()
    assert billing.inventory(db, access.user_id)["used_bytes"] == 0
    monkeypatch.setattr(storage, "delete_object", deleted.append)
    tasks.cleanup_workspace_files()
    db.expire_all()
    assert deleted == []
    assert billing.inventory(db, access.user_id)["used_bytes"] == 0


@pytest.mark.parametrize("value,expected", [(None, (0,9,False)), ("bytes=2-5", (2,5,True)), ("bytes=-3", (7,9,True)), ("bytes=8-", (8,9,True))])
def test_ranges(value, expected):
    assert byte_range(value, 10) == expected


@pytest.mark.parametrize("value", ["bytes=10-", "bytes=4-2", "bytes=0-1,3-4", "bytes=-0", "other=0-1"])
def test_invalid_ranges(value):
    with pytest.raises(HTTPException) as error:
        byte_range(value, 10)
    assert error.value.status_code == 416


def test_asgi_send_only_counts_successful_body(accounts):
    admin, _, _, db, a, b = setup(accounts)
    access = actor(a)
    identifier = billing.reserve_download(db, access, 6, "test")
    db.commit()
    response = MeteredResponse(io.BytesIO(b"abcdef"), 6, identifier, sessionmaker(bind=db.bind))
    messages = []
    async def send(message):
        messages.append(message)
    async def receive():
        await asyncio.Event().wait()
    asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    db.expire_all()
    row = db.get(StorageTransfer, identifier)
    assert row.sent_bytes == 6 and row.status == "completed"
    assert response.source.closed
    identifier = billing.reserve_download(db, access, 6, "failed-send")
    db.commit()
    response = MeteredResponse(io.BytesIO(b"abcdef"), 6, identifier, sessionmaker(bind=db.bind))
    async def fail(message):
        if message["type"] == "http.response.body":
            raise OSError("disconnected")
    with pytest.raises(Exception):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, fail))
    db.expire_all()
    row = db.get(StorageTransfer, identifier)
    assert row.sent_bytes == 0 and row.uncertain_bytes == 6 and row.status == "interrupted"


def test_authenticated_range_head_and_storage_failure(accounts, monkeypatch):
    from app.files import downloads as storage_downloads
    from app.conversations import attachments as chat_attachments
    from test_auth import register
    (client, _, _), db = accounts
    register(client)
    monkeypatch.setattr(chat_attachments, "upload_fileobj", lambda *_: None)
    item = client.post("/chat/attachments", files={"file": ("example.txt", b"0123456789", "text/plain")}).json()
    path = f"/chat/attachments/{item['id']}/content"
    reads = []
    def source(key, start, end):
        reads.append((start, end))
        return io.BytesIO(b"0123456789"[start:end+1])
    monkeypatch.setattr(storage_downloads, "download_range_fileobj", source)
    assert client.head(path).headers["content-length"] == "10"
    assert not reads and db.scalar(select(func.count()).select_from(StorageTransfer)) == 0
    response = client.get(path, headers={"Range":"bytes=3-6"})
    assert response.status_code == 206 and response.content == b"3456"
    assert response.headers["content-range"] == "bytes 3-6/10" and reads == [(3,6)]
    assert client.get(path, headers={"Range":"bytes=30-"}).status_code == 416
    def failed(*_):
        raise HTTPException(503, "文件暂不可用")
    monkeypatch.setattr(storage_downloads, "download_range_fileobj", failed)
    assert client.get(path).status_code == 503
    db.expire_all()
    grant = db.scalar(select(StorageEntitlement).where(StorageEntitlement.kind == "free"))
    assert grant.used_bytes == 4 and grant.reserved_bytes == 0
