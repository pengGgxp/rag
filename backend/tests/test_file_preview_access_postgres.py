from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, file_preview_access as preview
from app.models import CreditAccount, CreditLedgerEntry, User, Workspace, WorkspaceFile
from app.storage_models import StorageTransfer
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def test_parallel_grants_do_not_reserve_quota_or_touch_ledger(auth_postgres, monkeypatch):
    store = MemoryMount()
    monkeypatch.setattr(preview, "current_storage", lambda _: store)
    monkeypatch.setattr(store, "read_url", lambda *_, **__: "https://private.oss.example/test", raising=False)
    with Session(auth_postgres) as db:
        user = User(email=f"direct-preview-{uuid4()}@example.com", name="Preview")
        workspace = Workspace(name="Direct preview isolated")
        db.add_all([user, workspace]); db.flush()
        db.add(CreditAccount(workspace_id=workspace.id, balance=Decimal("12.3456"), lifetime_granted=Decimal("12.3456")))
        row = WorkspaceFile(workspace_id=workspace.id, path="报告.pdf", content_type="application/pdf",
            size_bytes=4, content_hash="test", created_by=user.id, updated_by=user.id,
            expires_at=auth.now()+timedelta(days=1))
        db.add(row); db.commit()
        access = auth.Access(user.id, workspace.id, "admin", "test")
        identifier, workspace_id = row.id, workspace.id
        store.objects[preview.object_key(workspace.id, row.path)] = b"%PDF"
    def grant(_):
        with Session(auth_postgres) as db:
            return preview.resource_access(identifier, preview.PreviewRequest(), Response(), db, access)["revision"]
    with ThreadPoolExecutor(4) as pool:
        revisions = list(pool.map(grant, range(12)))
    assert len(set(revisions)) == 1
    with Session(auth_postgres) as db:
        assert db.get(CreditAccount, workspace_id).balance == Decimal("12.3456")
        assert db.scalar(select(func.count()).select_from(StorageTransfer).where(StorageTransfer.workspace_id == workspace_id)) == 0
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(CreditLedgerEntry.workspace_id == workspace_id)) == 0
