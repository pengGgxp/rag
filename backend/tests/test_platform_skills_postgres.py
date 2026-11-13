"""Opt-in migration and concurrent writes on a disposable PostgreSQL database."""
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, main
from app.models import AgentSkill, AgentSkillFile, LoginSession, Membership, ProductSuggestion, SiteSkill, SiteSkillFile, User, Workspace
from test_auth_postgres import auth_postgres


def actor(engine):
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with Session(engine) as db:
        user = User(name="Skill admin", email=f"skill-{uuid4().hex}@example.com", is_site_admin=True,
                    email_verified_at=auth.now())
        workspace = Workspace(name="Skill test workspace")
        db.add_all([user, workspace]); db.flush()
        db.add(Membership(user_id=user.id, workspace_id=workspace.id, role="admin"))
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id,
            workspace_id=workspace.id, expires_at=auth.now()+timedelta(hours=1)))
        db.add(AgentSkill(workspace_id=workspace.id, owner_id=user.id, name="Private", description="Private",
            markdown="Never publish", files=[AgentSkillFile(path="private.txt", content=b"private")]))
        db.commit()
        return raw, csrf, user.id


def call(raw, csrf, method, path, body):
    with TestClient(main.app, headers={"X-RAG-Request": "1", "X-CSRF-Token": csrf}) as client:
        client.cookies.set(auth.COOKIE, raw)
        return client.request(method, path, json=body)


def test_parallel_suggestions_deduplicate_and_enforce_account_quota(auth_postgres):
    raw, csrf, user_id = actor(auth_postgres)
    body = {"request_id": str(uuid4()), "category": "feature", "content": "A suggestion"}
    barrier = Barrier(6)
    def submit(index):
        barrier.wait(timeout=10)
        return call(raw, csrf, "POST", "/suggestions", body)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(submit, range(6)))
    assert {result.status_code for result in results} == {201}
    assert len({result.json()["id"] for result in results}) == 1
    barrier = Barrier(6)
    def more(index):
        barrier.wait(timeout=10)
        return call(raw, csrf, "POST", "/suggestions", {**body, "request_id": str(uuid4())}).status_code
    with ThreadPoolExecutor(6) as pool:
        assert sorted(pool.map(more, range(6))) == [201, 201, 429, 429, 429, 429]
    with Session(auth_postgres) as db:
        assert db.scalar(select(func.count()).select_from(ProductSuggestion).where(ProductSuggestion.user_id == user_id)) == 3


def test_parallel_feedback_edits_preserve_versions_and_private_data(auth_postgres):
    raw, csrf, user_id = actor(auth_postgres)
    suggestion = call(raw, csrf, "POST", "/suggestions", {"request_id": str(uuid4()), "category": "other", "content": "Suggestion"}).json()
    for path, payload in ((f"/site/admin/suggestions/{suggestion['id']}", {"version": 1, "status": "processing"}),):
        barrier = Barrier(2)
        def edit(index):
            barrier.wait(timeout=10)
            return call(raw, csrf, "PUT", path, payload).status_code
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(edit, range(2))) == [200, 409]
    with Session(auth_postgres) as db:
        private = db.scalar(select(AgentSkill).where(AgentSkill.owner_id == user_id))
        assert private.markdown == "Never publish" and private.files[0].content == b"private"
        assert db.get(ProductSuggestion, UUID(suggestion["id"])).version == 2
