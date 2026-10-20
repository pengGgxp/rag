from uuid import UUID

from sqlalchemy import func, select

from app.models import AuditLog, IngestionJob, KnowledgeBase
from test_auth import accounts, library, register, share, switch
from test_retrieval import add_document


def test_admin_can_filter_workspace_audit_logs_and_members_cannot(accounts):
    (owner, viewer, outsider), db = accounts
    owner_session = register(owner)
    register(viewer, "viewer@example.com", "Viewer")
    register(outsider, "outsider@example.com", "Outsider")
    share(owner, "viewer@example.com")
    switch(viewer, owner_session["workspace_id"])

    kb_id = library(owner)
    document = add_document(db, db.get(KnowledgeBase, kb_id), "audit content")
    db.add(IngestionJob(document_id=document.id))
    db.commit()
    assert owner.delete(f"/documents/{document.id}").status_code == 200

    response = owner.get("/audit-logs?page=1&page_size=2&resource_type=document")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert payload["total"] == 1
    assert payload["items"][0]["action"] == "document.deleted"
    assert payload["items"][0]["resource_id"] == str(document.id)
    assert payload["items"][0]["actor"]["email"] == "owner@example.com"
    assert "content" not in str(payload["items"][0]["metadata"]).lower()

    assert viewer.get("/audit-logs").status_code == 403
    own_workspace = outsider.get("/audit-logs")
    assert own_workspace.status_code == 200
    assert own_workspace.json()["total"] == 1


def test_audit_logs_do_not_cross_workspace_and_metadata_is_safe(accounts):
    (owner, other, _), db = accounts
    first = register(owner)
    register(other, "other@example.com", "Other")
    kb_id = library(owner)
    owner_logs = owner.get("/audit-logs")
    assert owner_logs.status_code == 200
    assert owner_logs.json()["total"] >= 2
    assert other.post("/auth/workspace", json={"workspace_id": first["workspace_id"]}).status_code == 404
    assert other.get("/audit-logs").status_code == 200
    assert all(item["actor"]["email"] == "other@example.com"
               for item in other.get("/audit-logs").json()["items"])
    assert db.scalar(select(func.count()).select_from(AuditLog)) > 0
