"""Concurrent management requests against a disposable migrated PostgreSQL database."""
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, main
from app.models import LoginSession, Membership, SiteAuditEvent, UsagePolicy, User, Workspace
from test_auth_postgres import auth_postgres


def test_parallel_management_keeps_default_rows_and_policy_versions_unique(auth_postgres):
    engine = auth_postgres
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with Session(engine) as db:
        own, target = Workspace(name="Admin workspace"), Workspace(name="Unjoined target")
        actor = User(email=f"pricing-{uuid4().hex}@example.com", name="Pricing admin", is_site_admin=True, email_verified_at=auth.now())
        db.add_all([own, target, actor]); db.flush()
        db.add(Membership(user_id=actor.id, workspace_id=own.id, role="admin"))
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=actor.id,
            workspace_id=own.id, expires_at=auth.now()+timedelta(hours=1)))
        db.commit(); target_id = target.id
    path = f"/site/admin/workspaces/{target_id}/usage-policies"
    def parallel(method):
        barrier = Barrier(3)
        def run(_):
            with TestClient(main.app, headers={"X-RAG-Request":"1", "X-CSRF-Token":csrf}) as client:
                client.cookies.set(auth.COOKIE, raw)
                barrier.wait(timeout=10)
                response = client.get(path) if method == "GET" else client.put(path+"/llm_input_tokens", json={
                    "capability":"llm_input_tokens", "unit":"tokens", "unit_size":1000, "credits_per_unit":4, "enabled":True,
                })
                assert response.status_code == 200, response.text
                return response.json()
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(run, range(3)))
    parallel("GET")
    updates = parallel("PUT")
    assert sorted(row["version"] for row in updates) == [2,3,4]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(UsagePolicy).where(UsagePolicy.workspace_id == target_id)) == 4
        assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(
            SiteAuditEvent.target_id == str(target_id), SiteAuditEvent.action == "workspace.usage_policy_updated")) == 3
