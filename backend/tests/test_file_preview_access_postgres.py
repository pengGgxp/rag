from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import file_preview_access as preview

from app.identity import auth
from app.models import UserCreditAccount, CreditLedgerEntry, User, WorkspaceFile, WorkspaceFileVersion
from app.shared_resources import object_key
from app.storage_models import StorageTransfer
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def test_parallel_grants_do_not_reserve_quota_or_touch_ledger(auth_postgres, monkeypatch):
    store = MemoryMount()
    monkeypatch.setattr(preview, "current_storage", lambda _: store)
    monkeypatch.setattr(store, "read_url", lambda *_, **__: "https://private.oss.example/test", raising=False)
    with Session(auth_postgres) as db:
        user = User(email=f"direct-preview-{uuid4()}@example.com", name="Preview")
        db.add(user); db.flush()
        db.add(UserCreditAccount(user_id=user.id, balance=Decimal("12.3456"), lifetime_granted=Decimal("12.3456")))
        row = WorkspaceFile(account_id=user.id, path="报告.pdf", content_type="application/pdf",
            size_bytes=4, content_hash="test", created_by=user.id, updated_by=user.id,
            expires_at=auth.now()+timedelta(days=1))
        key = object_key(user.id, row.path)
        row.versions.append(WorkspaceFileVersion(account_id=user.id, version=1, object_key=key,
            content_type=row.content_type, size_bytes=row.size_bytes, content_hash=row.content_hash,
            created_by=user.id))
        db.add(row); db.commit()
        access = auth.Access(user.id, "test")
        identifier, account_id = row.id, user.id
        store.objects[key] = b"%PDF"
    def grant(_):
        with Session(auth_postgres) as db:
            return preview.resource_access(identifier, preview.PreviewRequest(), Response(), db, access)["revision"]
    with ThreadPoolExecutor(4) as pool:
        revisions = list(pool.map(grant, range(12)))
    assert len(set(revisions)) == 1
    with Session(auth_postgres) as db:
        assert db.get(UserCreditAccount, account_id).balance == Decimal("12.3456")
        assert db.scalar(select(func.count()).select_from(StorageTransfer).where(StorageTransfer.account_id == account_id)) == 0
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(CreditLedgerEntry.account_id == account_id)) == 0
