"""Opt-in tests against a disposable, fully migrated PostgreSQL database only."""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import auth, email_auth, main, site_auth
from app.models import AuthenticationFlow, CreditLedgerEntry, ExternalIdentity, LoginSession, User


@pytest.fixture
def auth_postgres(monkeypatch):
    url = os.environ.get("AUTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires AUTH_TEST_DATABASE_URL for a disposable migrated PostgreSQL database")
    if not (make_url(url).database or "").startswith("rag_auth_test_"):
        pytest.fail("refusing non-test database")
    engine = create_engine(url, pool_size=10)
    def database():
        with Session(engine, autoflush=False) as db:
            yield db
    main.app.dependency_overrides[main.get_db] = database
    @asynccontextmanager
    async def isolated_lifespan(_app):
        yield
    monkeypatch.setattr(main.app.router, "lifespan_context", isolated_lifespan)
    settings = auth.get_settings().model_copy(update={"auth_secret_key": SecretStr("test-only-secret-with-at-least-32-chars")})
    monkeypatch.setattr(email_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(email_auth, "email_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr(auth, "rate_limit", lambda *_a, **_k: None)
    try:
        yield engine
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
        engine.dispose()


def add_flow(engine, *, email=None, purpose="register", user_id=None, stage="profile", identity=None):
    raw = uuid4().hex + uuid4().hex
    with Session(engine) as db:
        row = AuthenticationFlow(token_hash=auth.digest(raw), purpose=purpose, email=email or f"pg-{uuid4().hex}@example.com",
            user_id=user_id, data={"stage": stage, **({"identity": identity} if identity else {})},
            expires_at=auth.now()+timedelta(minutes=20))
        db.add(row); db.commit()
    return raw


def test_concurrent_verified_registration_consumes_flow_and_grants_once(auth_postgres):
    engine = auth_postgres
    email = f"concurrent-{uuid4().hex}@example.com"
    raw = add_flow(engine, email=email)
    barrier = Barrier(4)
    def register(_):
        with TestClient(main.app, headers={"X-RAG-Request": "1"}) as client:
            client.cookies.set(email_auth.FLOW_COOKIE, raw)
            barrier.wait(timeout=10)
            return client.post("/auth/complete-registration", json={"name": "Concurrent", "password": "test-password-1234"}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(register, range(4)))
    assert codes.count(201) == 1 and set(codes) <= {201, 409}
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.email == email)) == 1
        user = db.scalar(select(User).where(User.email == email))
        session = db.scalar(select(LoginSession).where(LoginSession.user_id == user.id))
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == session.user_id)) == 1
        assert db.get(AuthenticationFlow, auth.digest(raw)) is None


def test_external_identity_conflict_rolls_back_user_workspace_and_credit(auth_postgres):
    engine = auth_postgres
    subject = uuid4().hex
    first_email = f"first-{subject}@example.com"
    second_email = f"second-{subject}@example.com"
    identity = {"provider": "senyu:u.arsn.cn:pgtest:wx", "subject": subject}
    with TestClient(main.app, headers={"X-RAG-Request": "1"}) as client:
        for email, expected in ((first_email, 201), (second_email, 409)):
            client.cookies.set(email_auth.FLOW_COOKIE, add_flow(engine, email=email, purpose="wechat", identity=identity))
            response = client.post("/auth/complete-registration", json={"name": "WeChat"})
            assert response.status_code == expected, response.text
    with Session(engine) as db:
        assert db.scalar(select(User).where(User.email == second_email)) is None
        assert db.scalar(select(func.count()).select_from(ExternalIdentity).where(ExternalIdentity.subject == subject)) == 1


def test_concurrent_email_verification_signs_in_once(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"proof-{uuid4().hex}@example.com", "Proof", "test-password-1234")
        db.commit(); user_id, email = user.id, user.email
    raw = add_flow(engine, purpose="login", user_id=user_id, email=email, stage="code")
    with Session(engine) as db:
        row = db.get(AuthenticationFlow, auth.digest(raw))
        row.code_hash = email_auth.code_digest(row, "345678")
        row.code_expires_at = auth.now()+timedelta(minutes=10); db.commit()
    barrier = Barrier(4)
    def verify(_):
        with TestClient(main.app, headers={"X-RAG-Request": "1"}) as client:
            client.cookies.set(email_auth.FLOW_COOKIE, raw)
            barrier.wait(timeout=10)
            return client.post("/auth/email/verify", json={"code": "345678"}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(verify, range(4)))
    assert codes.count(200) == 1 and set(codes) <= {200, 409}
    with Session(engine) as db:
        user = db.get(User, user_id)
        assert user.email_verified_at and user.password_hash is None
        assert db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == user_id)) == 1
