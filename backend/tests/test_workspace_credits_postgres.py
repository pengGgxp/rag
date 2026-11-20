"""Manual ledger adjustments on the disposable migrated PostgreSQL database."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, main
from app.credits import adjust_balance
from app.models import UserCreditAccount, CreditLedgerEntry, SiteAuditEvent
from app.site_audit import record
from test_auth_postgres import auth_postgres


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_adjustments_lock_balance_and_retry_once(auth_postgres, same_request):
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"credits-{uuid4().hex}@example.com", "Admin")
        workspace = user.id
        user.is_site_admin = True
        response = auth.login_response(db, user)
        csrf = json.loads(response.body)["csrf_token"]
        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        initial = db.get(UserCreditAccount, workspace).balance
    barrier = Barrier(2)
    common_id = str(uuid4())
    def debit(_):
        with TestClient(main.app, headers={"X-RAG-Request": "1", "X-CSRF-Token": csrf}) as client:
            client.cookies.set(auth.COOKIE, cookie)
            barrier.wait(timeout=10)
            return client.post(f"/site/admin/users/{workspace}/credits/adjustments", json={
                "request_id": common_id if same_request else str(uuid4()), "direction": "deduct",
                "amount": str(initial), "expected_balance": str(initial), "reason": "Concurrent verification",
            }).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(debit, range(2))) == ([200, 200] if same_request else [200, 409])
    with Session(auth_postgres) as db:
        account = db.get(UserCreditAccount, workspace)
        assert account.balance == 0 and account.lifetime_spent == initial
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == workspace, CreditLedgerEntry.kind == "manual_debit")) == 1
        assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(
            SiteAuditEvent.target_id == str(workspace), SiteAuditEvent.action == "user.credits_adjusted")) == 1


def test_rollback_restores_account_ledger_and_audit(auth_postgres):
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"rollback-{uuid4().hex}@example.com", "Admin")
        workspace = user.id
        db.commit()
        user_id, balance = user.id, db.get(UserCreditAccount, workspace).balance
    with Session(auth_postgres) as db:
        account, entry, _ = adjust_balance(db, workspace, user_id, uuid4(), 100, "Rollback verification", balance)
        record(db, db.get(type(user), user_id), "user.credits_adjusted", "user", workspace)
        db.flush()
        assert account.balance == balance + 100
        db.rollback()
    with Session(auth_postgres) as db:
        assert db.get(UserCreditAccount, workspace).balance == balance
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == workspace, CreditLedgerEntry.kind == "manual_credit")) == 0
        assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(
            SiteAuditEvent.target_id == str(workspace))) == 0
