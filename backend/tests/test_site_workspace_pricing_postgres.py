"""Concurrent management requests against a disposable migrated PostgreSQL database."""
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import main

from app.identity import auth
from app.models import LoginSession, SiteAuditEvent, UsagePolicy, User, UserModelPolicy
from test_auth_postgres import auth_postgres


def test_parallel_multiplier_management_keeps_versions_unique_without_price_copies(auth_postgres):
    engine = auth_postgres
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with Session(engine) as db:
        target = User(email=f"target-{uuid4().hex}@example.com", name="Pricing target")
        actor = User(email=f"pricing-{uuid4().hex}@example.com", name="Pricing admin", is_site_admin=True, email_verified_at=auth.now())
        db.add_all([target, actor]); db.flush()
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=actor.id,
            expires_at=auth.now()+timedelta(hours=1)))
        db.commit(); target_id = target.id
    path = f"/site/admin/users/{target_id}"
    policy = {"billing_multiplier_percent": 125, "version": 0, "available_models": [], "default_model": None}
    def parallel(method):
        barrier = Barrier(3)
        def run(_):
            with TestClient(main.app, headers={"X-RAG-Request":"1", "X-CSRF-Token":csrf}) as client:
                client.cookies.set(auth.COOKIE, raw)
                barrier.wait(timeout=10)
                response = client.get(path) if method == "GET" else client.put(f"{path}/model-policy", json=policy)
                assert response.status_code in (200, 409), response.text
                return response.status_code
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(run, range(3)))
    parallel("GET")
    updates = parallel("PUT")
    assert sorted(updates) == [200,409,409]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(UsagePolicy).where(UsagePolicy.account_id == target_id)) == 0
        assert db.get(UserModelPolicy, target_id).billing_multiplier_percent == 125
        assert db.get(UserModelPolicy, target_id).version == 1
        assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(
            SiteAuditEvent.target_id == str(target_id), SiteAuditEvent.action == "user.model_policy_changed")) == 1
