from uuid import UUID

from test_auth import library, register
from app.models import Document, DocumentStatus


def test_document_preview_is_loaded_on_demand_and_hides_deleted_content(accounts):
    (owner, _, _), session = accounts
    register(owner)
    knowledge_base_id = library(owner)
    created = owner.post(
        f"/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("notes.txt", b"preview content", "text/plain")},
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]

    document = session.get(Document, UUID(document_id))
    document.parsed_text = "preview content"
    document.status = DocumentStatus.READY
    session.commit()

    listed = owner.get(f"/knowledge-bases/{knowledge_base_id}/documents")
    assert listed.status_code == 200
    assert "parsed_text" not in listed.json()[0]

    preview = owner.get(f"/documents/{document_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["parsed_text"] == "preview content"

    deleted = owner.delete(f"/documents/{document_id}")
    assert deleted.status_code == 200
    deleted_preview = owner.get(f"/documents/{document_id}/preview")
    assert deleted_preview.status_code == 200
    assert deleted_preview.json()["parsed_text"] is None


def test_document_preview_respects_workspace_access(accounts):
    (owner, outsider, _), _session = accounts
    register(owner)
    knowledge_base_id = library(owner)
    created = owner.post(
        f"/knowledge-bases/{knowledge_base_id}/documents",
        files={"file": ("notes.txt", b"private preview", "text/plain")},
    )
    assert created.status_code == 201
    register(outsider, email="outsider@example.com", name="Outsider")
    assert outsider.get(f"/documents/{created.json()['id']}/preview").status_code == 404
