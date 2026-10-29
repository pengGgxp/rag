from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, llm_providers, main, shared_providers
from app.models import ModelBillingSettings, SiteModelProvider, ThinkingTest, Workspace
from auth_helpers import configured_model
from test_auth_postgres import auth_postgres
from test_thinking import effort


@pytest.mark.parametrize("metered", [False, True])
def test_postgres_concurrent_probe_calls_upstream_and_charges_once(auth_postgres, monkeypatch, metered):
    entered, release = Event(), Event()
    calls = []
    with Session(auth_postgres) as db:
        import secrets
        from datetime import timedelta
        from app.models import CreditAccount, LoginSession, Membership, User
        workspace = Workspace(name=f"Thinking probe {uuid4()}")
        user = User(email=f"thinking-{uuid4()}@example.com", name="Thinking test", is_site_admin=True,
            email_verified_at=auth.now())
        db.add_all([workspace, user]); db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        raw, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=auth.digest(raw), user_id=user.id, workspace_id=workspace.id,
            csrf_token=csrf, expires_at=auth.now()+timedelta(hours=1)))
        db.add(CreditAccount(workspace_id=workspace.id, balance=1000, lifetime_granted=1000))
        settings = db.get(ModelBillingSettings, 1)
        if settings is None:
            settings = ModelBillingSettings(id=1, enabled=metered, shared_pricing_enabled=True, template={})
            db.add(settings)
        else:
            settings.enabled, settings.shared_pricing_enabled = metered, True
        binding = configured_model(db, workspace.id)
        row = db.get(SiteModelProvider, binding.site_provider_id)
        row.thinking_config, row.thinking_version = effort("bailian"), 1
        identifier, version = row.id, row.version
        db.commit()
        workspace_id = workspace.id
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda _: "test-only")
    monkeypatch.setattr(shared_providers, "assert_available", lambda _: None)
    @contextmanager
    def streaming(*_args, **kwargs):
        calls.append(kwargs["json"])
        entered.set()
        assert release.wait(10)
        yield httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"choices":[{"delta":{"reasoning_content":"not logged","content":"391"},"finish_reason":"stop"}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"completion_tokens_details":{"reasoning_tokens":15}}}\n\n'))
    monkeypatch.setattr(httpx, "stream", streaming)
    payload = {"request_id": str(uuid4()), "version": version, "thinking_config_version": 1}
    url = f"/site/admin/llm-providers/{identifier}/thinking/test"
    def probe():
        with TestClient(main.app, headers={"X-RAG-Request": "1", "X-CSRF-Token": csrf}) as client:
            client.cookies.set(auth.COOKIE, raw)
            return client.post(url, json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(probe)
        assert entered.wait(10)
        second = pool.submit(probe).result(timeout=10)
        assert second.status_code == 409
        release.set()
        completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert probe().json() == completed.json()
    assert len(calls) == 1
    with Session(auth_postgres) as db:
        from app.models import CreditLedgerEntry
        saved = db.get(ThinkingTest, UUID(payload["request_id"]))
        assert saved.status == "completed" and "not logged" not in str(saved.result)
        charges = db.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == workspace_id, CreditLedgerEntry.kind == "usage_settlement")).all()
        assert len(charges) == 1
