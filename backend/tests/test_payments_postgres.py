"""Opt-in locking tests; run only against a disposable recharge test database."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import main
from app.payments import orders as payments

from app.identity import auth
from app.identity.auth import Access, now
from app.infra.config import Settings
from app.models import (
    AuditLog, UserCreditAccount, CreditLedgerEntry, LoginSession, RechargeOrder, User,
)


@pytest.fixture
def postgres():
    url = os.environ.get("PAYMENTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires PAYMENTS_TEST_DATABASE_URL for a migrated disposable PostgreSQL database")
    if not (make_url(url).database or "").startswith("rag_recharge_test_"):
        pytest.fail("refusing to run against a non-test database")
    engine = create_engine(url, pool_size=12, max_overflow=0)
    workspace_id = user_id = uuid4()
    token = uuid4().hex
    settings = Settings(_env_file=None).model_copy(update={
        "zhifux_enabled": True, "zhifux_merchant_num": "888888",
        "zhifux_secret": SecretStr("test-only-secret"),
        "zhifux_api_base_url": "https://gateway.example", "zhifux_pay_type": "aloop",
        "api_public_url": "https://api.example", "frontend_url": "https://app.example",
        "recharge_packages_json": '[{"id":"test","name":"test","amount_fen":1234,"credits":567}]',
        "zhifux_custom_recharge_enabled": True,
    })
    with Session(engine) as db:
        db.add(User(id=user_id, email=f"pg-{user_id}@example.com", name="Payment test"))
        db.flush()
        db.add(UserCreditAccount(user_id=workspace_id, balance=1000, lifetime_granted=1000, lifetime_spent=0))
        db.add(LoginSession(token_hash=token, user_id=user_id,
                            csrf_token="test", expires_at=now() + timedelta(hours=1)))
        db.commit()
    try:
        yield engine, workspace_id, user_id, settings, token
    finally:
        engine.dispose()


def add_order(engine, workspace_id, user_id):
    id = uuid4()
    with Session(engine) as db:
        db.add(RechargeOrder(
            id=id, account_id=workspace_id, user_id=user_id, idempotency_key=id.hex,
            package_id="test", package_name="test", amount_fen=1234, credits=567,
            merchant_num="888888", pay_type="aloop", platform_order_no="P" + id.hex, status="pending",
        ))
        db.commit()
    return id


def params(order_id, settings):
    result = {
        "state": "1", "merchantNum": "888888", "orderNo": order_id.hex,
        "amount": "12.34", "platformOrderNo": "P" + order_id.hex,
    }
    result["sign"] = payments.md5_sign("1888888" + order_id.hex + "12.34" + settings.zhifux_secret.get_secret_value())
    return result


def test_duplicate_callbacks_concurrently_credit_once(postgres):
    engine, workspace_id, user_id, settings, _ = postgres
    order_id = add_order(engine, workspace_id, user_id)
    barrier = Barrier(8)

    def pay(_):
        with Session(engine) as db:
            barrier.wait(timeout=15)
            payments.accept_payment(db, params(order_id, settings), settings)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(pay, range(8)))
    with Session(engine) as db:
        account = db.get(UserCreditAccount, workspace_id)
        assert account.balance == account.lifetime_granted == 1567
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == workspace_id,
        )) == 1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.owner_user_id == workspace_id, AuditLog.action == "recharge.paid",
        )) == 1


def test_different_orders_concurrently_do_not_lose_balance_updates(postgres):
    engine, workspace_id, user_id, settings, _ = postgres
    ids = [add_order(engine, workspace_id, user_id) for _ in range(8)]
    barrier = Barrier(8)

    def pay(order_id):
        with Session(engine) as db:
            # Simulate an earlier account read in the same request/session.
            account = db.get(UserCreditAccount, workspace_id)
            assert account.balance == 1000
            barrier.wait(timeout=15)
            payments.accept_payment(db, params(order_id, settings), settings)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(pay, ids))
    with Session(engine) as db:
        account = db.get(UserCreditAccount, workspace_id)
        assert account.balance == account.lifetime_granted == 1000 + 8 * 567
        balances = db.scalars(select(CreditLedgerEntry.balance_after).where(
            CreditLedgerEntry.account_id == workspace_id,
        )).all()
        assert sorted(balances) == [1000 + n * 567 for n in range(1, 9)]


@pytest.mark.parametrize("custom", [False, True])
def test_concurrent_identical_creations_call_gateway_once(postgres, monkeypatch, custom):
    engine, workspace_id, user_id, settings, token = postgres
    calls = []
    lock = Lock()
    barrier = Barrier(8)

    def database():
        with Session(engine, autoflush=False) as db:
            yield db

    def start(_settings, order):
        with lock:
            calls.append(order.id)
        return payments.Checkout("P" + order.id.hex, "https://checkout.example/pay")

    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(payments, "start_checkout", start)
    main.app.dependency_overrides[main.get_db] = database
    main.app.dependency_overrides[auth.get_access] = lambda: Access(user_id, token)
    key = uuid4().hex
    quote = payments.package_quote(payments.configured_packages(settings)[0])
    payload = {"package_id": "custom_amount" if custom else "test", "idempotency_key": key, "quote": quote,
               "payment_method_id": "zhifux_aloop"}
    if custom:
        payload["custom_amount_fen"] = 2300
        payload["quote"] = payments.recharge_options(settings)[1]["quote"]

    def submit(_):
        client = TestClient(main.app, headers={"X-RAG-Request": "1"})
        try:
            barrier.wait(timeout=15)
            response = client.post("/payments/orders", json=payload)
            assert response.status_code == 201, response.text
            return response.json()["id"]
        finally:
            client.close()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(8)))
        assert len(set(ids)) == len(calls) == 1
        with Session(engine) as db:
            order = db.get(RechargeOrder, calls[0])
            assert order.amount_fen == (2300 if custom else 1234)
            assert order.credits == (2300 if custom else 567)
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
        main.app.dependency_overrides.pop(auth.get_access, None)
