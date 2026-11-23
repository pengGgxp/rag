from uuid import UUID

from sqlalchemy import func, select

from app.models import (
    AuditLog, IngestionJob, KnowledgeBase, User,
)
from test_auth import accounts, library, register
from test_retrieval import add_document
from app.audit_log import record_audit


def test_users_can_filter_their_own_audit_logs_without_workspace_members(accounts, monkeypatch):
    (owner, viewer, outsider), db = accounts
    register(owner)
    register(viewer, "viewer@example.com", "Viewer")
    register(outsider, "outsider@example.com", "Outsider")

    kb_id = library(owner)
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    document = add_document(db, db.get(KnowledgeBase, kb_id), "audit content", store=store)
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

    viewer_logs = viewer.get("/audit-logs")
    assert viewer_logs.status_code == 200
    assert viewer_logs.json()["total"] >= 1
    own_logs = outsider.get("/audit-logs")
    assert own_logs.status_code == 200
    assert own_logs.json()["total"] >= 1


def test_audit_logs_do_not_cross_workspace_and_metadata_is_safe(accounts):
    (owner, other, _), db = accounts
    register(owner)
    register(other, "other@example.com", "Other")
    kb_id = library(owner)
    owner_logs = owner.get("/audit-logs")
    assert owner_logs.status_code == 200
    assert owner_logs.json()["total"] >= 2
    assert other.get("/audit-logs").status_code == 200
    assert all(item["actor"]["email"] == "other@example.com"
               for item in other.get("/audit-logs").json()["items"])
    assert db.scalar(select(func.count()).select_from(AuditLog)) > 0


def test_audit_history_is_read_by_user_across_legacy_workspaces(accounts):
    (owner, outsider, _), db = accounts
    identity = register(owner)
    outsider_identity = register(outsider, "other@example.com", "Other")
    owner_id = UUID(identity["user"]["id"])
    record_audit(
        db,
        owner_user_id=owner_id,
        actor_user_id=UUID(outsider_identity["user"]["id"]),
        action="legacy.resource_updated",
        resource_type="resource",
        metadata={"source": "historical workspace"},
    )
    db.commit()

    response = owner.get("/audit-logs?action=legacy.resource_updated")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["actor"]["email"] == "other@example.com"
    assert outsider.get("/audit-logs?action=legacy.resource_updated").json()["total"] == 0
