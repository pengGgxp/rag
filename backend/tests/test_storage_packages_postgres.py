"""Run only against a disposable migrated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import storage_billing as billing
from app.auth import Access, now
from app.models import CreditAccount, CreditLedgerEntry, User, Workspace, WorkspaceFile, WorkspaceFileVersion
from app.storage_models import StorageEntitlement, StorageQuotaSettings
from test_auth_postgres import auth_postgres


def setup(engine):
    with Session(engine) as db:
        user = User(email=f"storage-{uuid4().hex}@example.com", name="Storage test", is_site_admin=True)
        workspace = Workspace(name="Storage isolated test", billing_multiplier_percent=500)
        db.add_all([user, workspace]); db.flush()
        db.add(CreditAccount(workspace_id=workspace.id, balance=10, lifetime_granted=10, lifetime_spent=0))
        config = billing.settings(db, lock=True)
        config.version += 1
        config.free_storage_bytes = 10
        config.free_download_bytes = 10
        config.packages = [{"id":"test", "name":"Test", "storage_bytes":20, "download_bytes":10,
            "days":30, "price_credits":"3.1234", "enabled":True, "order":0}]
        db.add(config); db.commit()
        return Access(user.id, workspace.id, "admin", ""), config.version


def test_concurrent_purchase_debits_once_and_preserves_price_snapshot(auth_postgres):
    access, version = setup(auth_postgres)
    payload = billing.Purchase(package_id="test", version=version, request_id=uuid4())
    gate = Barrier(2)
    def buy(_):
        with Session(auth_postgres, autoflush=False) as db:
            gate.wait()
            return billing.purchase(db, access, payload)["id"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(buy, range(2)))
    assert results[0] == results[1]
    with Session(auth_postgres) as db:
        assert db.get(CreditAccount, access.workspace_id).balance == Decimal("6.8766")
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == access.workspace_id, CreditLedgerEntry.kind == "storage_package_purchase")) == 1
        row = db.scalar(select(StorageEntitlement).where(StorageEntitlement.workspace_id == access.workspace_id))
        assert row.snapshot["price_credits"] == "3.1234" and row.storage_bytes == 20


def test_concurrent_writes_and_transfers_cannot_exceed_allowance(auth_postgres):
    access, _ = setup(auth_postgres)
    gate = Barrier(2)
    def write(index):
        with Session(auth_postgres, autoflush=False) as db:
            gate.wait()
            try:
                billing.check_capacity(db, access.workspace_id, 7)
                row = WorkspaceFile(workspace_id=access.workspace_id, path=f"test-{index}", content_hash="x",
                    created_by=access.user_id, updated_by=access.user_id, expires_at=now()+timedelta(days=3))
                db.add(row); db.flush()
                db.add(WorkspaceFileVersion(file_id=row.id, workspace_id=access.workspace_id, version=1,
                    object_key=f"test/{uuid4()}", size_bytes=7, content_hash="x", content_type="text/plain", created_by=access.user_id))
                db.commit()
                return "saved"
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(map(str, pool.map(write, range(2)))) == ["413", "saved"]
    gate = Barrier(2)
    def reserve(_):
        with Session(auth_postgres, autoflush=False) as db:
            gate.wait()
            try:
                billing.reserve_download(db, access, 7, "concurrent")
                db.commit()
                return "reserved"
            except HTTPException as error:
                db.rollback()
                return error.status_code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(map(str, pool.map(reserve, range(2)))) == ["402", "reserved"]


def test_entitlement_and_debit_roll_back_together(auth_postgres):
    access, version = setup(auth_postgres)
    with Session(auth_postgres) as db:
        account = db.get(CreditAccount, access.workspace_id)
        account.balance = 0
        db.commit()
        with pytest.raises(HTTPException):
            billing.purchase(db, access, billing.Purchase(package_id="test", version=version, request_id=uuid4()))
        db.rollback()
        assert db.scalar(select(func.count()).select_from(StorageEntitlement).where(
            StorageEntitlement.workspace_id == access.workspace_id)) == 0
        assert db.get(CreditAccount, access.workspace_id).balance == 0
