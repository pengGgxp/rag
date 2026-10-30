from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models import ProductSuggestion, SiteAuditEvent
from test_auth import register, share, switch
from test_shared_providers import setup_site


def test_suggestion_retry_quota_permissions_and_processing(accounts, monkeypatch):
    admin, db, identity = setup_site(accounts, monkeypatch)
    (_, user, anonymous), _ = accounts
    member = register(user, "suggestion@example.com")
    body = {"request_id": str(uuid4()), "category": "feature", "content": "支持批量导出 <script>alert(1)</script>"}
    assert anonymous.post("/suggestions", json=body).status_code == 401
    first = user.post("/suggestions", json=body)
    assert first.status_code == 201
    assert user.post("/suggestions", json=body).json() == first.json()
    assert user.post("/suggestions", json={**body, "content": "different"}).status_code == 409
    share(admin, member["user"]["email"], "viewer")
    switch(user, identity["workspace_id"])
    for _ in range(2):
        assert user.post("/suggestions", json={**body, "request_id": str(uuid4())}).status_code == 201
    assert user.post("/suggestions", json={**body, "request_id": str(uuid4())}).status_code == 429
    assert user.post("/suggestions", json=body).json() == first.json()  # Retry does not consume quota.
    rows = db.scalars(select(ProductSuggestion).order_by(ProductSuggestion.created_at, ProductSuggestion.id)).all()
    assert len(rows) == 3
    original = db.get(ProductSuggestion, UUID(first.json()["id"]))
    assert str(original.workspace_id) == member["workspace_id"]
    assert user.get("/site/admin/suggestions").status_code == 403
    assert user.get(f"/site/admin/suggestions/{original.id}").status_code == 403
    assert user.get("/suggestions").status_code == 405
    result = admin.get("/site/admin/suggestions?q=批量&category=feature&status=pending&page_size=1").json()
    assert result["total"] == 3 and len(result["items"]) == 1
    detail = admin.get(f"/site/admin/suggestions/{original.id}").json()
    assert detail["content"] == body["content"] and detail["version"] == 1
    changed = admin.put(f"/site/admin/suggestions/{original.id}", json={"status": "processing", "version": 1})
    assert changed.status_code == 200 and changed.json()["handled_by"]
    assert admin.put(f"/site/admin/suggestions/{original.id}", json={"status": "resolved", "version": 1}).status_code == 409
    assert user.put(f"/site/admin/suggestions/{original.id}", json={"status": "resolved", "version": 2}).status_code == 403
    assert admin.put(f"/site/admin/suggestions/{original.id}", json={"status": "resolved", "version": 2}).status_code == 200
    assert admin.get("/site/admin/suggestions?status=resolved").json()["total"] == 1
    events = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.target_type == "suggestion")).all()
    assert len(events) == 2 and "script" not in str([event.details for event in events])
    for row in rows:
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    db.commit()
    assert user.post("/suggestions", json={**body, "request_id": str(uuid4())}).status_code == 201


@pytest.mark.parametrize("change", [{"content": " "}, {"content": "x" * 2001}, {"category": "unknown"},
    {"conversation_id": str(uuid4())}, {"content": "okay", "request_id": "bad"}])
def test_suggestion_validation(accounts, change):
    (user, _, _), _ = accounts
    register(user)
    body = {"request_id": str(uuid4()), "category": "feature", "content": "suggestion", **change}
    assert user.post("/suggestions", json=body).status_code == 422
