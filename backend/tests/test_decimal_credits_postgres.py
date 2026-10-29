"""Four-decimal accounting against the disposable, migrated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import model_billing
from app.credits import _entry, adjust_balance, authorization_hold, ensure_account, recharge, release_hold
from app.models import CreditAccount, CreditLedgerEntry, ModelUsageRequest, Workspace
from test_auth_postgres import auth_postgres


def workspace(engine, balance):
    with Session(engine) as db:
        row = Workspace(name="Decimal acceptance")
        db.add(row)
        db.flush()
        account = ensure_account(db, row.id, initial=1000)
        account.balance = Decimal(balance)
        db.commit()
        return row.id


def test_numeric_schema_and_integer_history_preserved(auth_postgres, monkeypatch):
    identifier = workspace(auth_postgres, "1000")
    with Session(auth_postgres) as db:
        columns = db.execute(text("""
            SELECT numeric_scale FROM information_schema.columns
            WHERE table_name = 'credit_accounts'
              AND column_name IN ('balance', 'lifetime_granted', 'lifetime_spent')
        """)).scalars().all()
        assert columns == [4, 4, 4]
        before = db.scalar(select(CreditLedgerEntry.amount).where(
            CreditLedgerEntry.workspace_id == identifier))
        assert before == Decimal("1000.0000")
    monkeypatch.setenv("DATABASE_URL", str(auth_postgres.url.render_as_string(hide_password=False)))
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()
    with Session(auth_postgres) as db:
        assert db.get(CreditAccount, identifier).balance == before


def test_concurrent_fractional_debits_never_overdraw(auth_postgres):
    identifier = workspace(auth_postgres, "0.0003")
    barrier = Barrier(4)
    def debit(_):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                _entry(db, ensure_account(db, identifier), actor_user_id=None,
                    amount=Decimal("-0.0001"), kind="usage_settlement",
                    idempotency_key=f"decimal:{uuid4()}", metadata={})
                db.commit()
                return "paid"
            except HTTPException as exc:
                db.rollback()
                assert exc.status_code == 402
                return "insufficient"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(debit, range(4)))
    assert results.count("paid") == 3 and results.count("insufficient") == 1
    with Session(auth_postgres) as db:
        account = db.get(CreditAccount, identifier)
        assert account.balance == 0 and account.lifetime_spent == Decimal("0.0003")


def test_fractional_pending_hold_and_recharge_remain_exact(auth_postgres):
    identifier = workspace(auth_postgres, "1.4210")
    request = uuid4()
    with Session(auth_postgres) as db:
        authorization_hold(db, identifier, None, request, 1)
        assert db.get(CreditAccount, identifier).balance == Decimal("0.4210")
        release_hold(db, identifier, None, request, 1)
        release_hold(db, identifier, None, request, 1)
        db.add(ModelUsageRequest(request_id=request, workspace_id=identifier,
            credits=Decimal("1.4211"), hold=0, status="pending"))
        db.commit()
        with pytest.raises(HTTPException):
            model_billing.settle_pending(db, identifier)
        db.rollback()
        assert model_billing.pending_credits(db, identifier) == Decimal("1.4211")
        recharge(db, identifier, None, 1, uuid4(), 100)
        model_billing.settle_pending(db, identifier)
        db.commit()
        model_billing.settle_pending(db, identifier)
        db.commit()
        account = db.get(CreditAccount, identifier)
        assert account.balance == Decimal("0.9999")
        assert account.lifetime_spent == Decimal("1.4211")
        assert model_billing.pending_credits(db, identifier) == 0
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.idempotency_key == f"model:{request}:charge")) == 1


def test_fractional_admin_balance_conflict_and_rollback(auth_postgres):
    identifier = workspace(auth_postgres, "0.1234")
    with Session(auth_postgres) as db:
        with pytest.raises(HTTPException) as error:
            adjust_balance(db, identifier, None, uuid4(), Decimal("0.0001"), "Test", Decimal("0.123"))
        assert error.value.status_code == 409
        db.rollback()
        account, _, _ = adjust_balance(db, identifier, None, uuid4(), Decimal("-0.1234"),
            "Test", Decimal("0.1234"))
        assert account.balance == 0
        db.rollback()
        assert db.get(CreditAccount, identifier).balance == Decimal("0.1234")


def test_downgrade_refuses_to_destroy_fractional_credits(auth_postgres, monkeypatch):
    identifier = workspace(auth_postgres, "0.0001")
    monkeypatch.setenv("DATABASE_URL", auth_postgres.url.render_as_string(hide_password=False))
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="Decimal credits exist"):
            command.downgrade(Config("alembic.ini"), "0035_chat_attachments")
    finally:
        get_settings.cache_clear()
    with Session(auth_postgres) as db:
        assert db.get(CreditAccount, identifier).balance == Decimal("0.0001")
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0036_decimal_credits"
