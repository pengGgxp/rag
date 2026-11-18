import io
from uuid import UUID

from sqlalchemy import select

from app import auth, file_preview_access as preview
from app.models import ChatAttachment, Document, DocumentStatus, KnowledgeBase, UsageEvent
from app.storage_models import StorageTransfer
from test_auth import library, register, share, switch
from test_preview_fixtures import resource, store


def signed_storage(monkeypatch, store):
    signed = []
    monkeypatch.setattr(preview, "current_storage", lambda _: store)
    def sign(key, seconds, inline):
        signed.append((key, seconds, inline))
        return "https://private.oss.example/temporary-capability"
    monkeypatch.setattr(store, "read_url", sign, raising=False)
    monkeypatch.setattr(store, "download", lambda _: (_ for _ in ()).throw(AssertionError("preview read body")))
    return signed


def test_resource_capability_checks_permissions_revision_and_never_meters(accounts, store, monkeypatch):
    (owner, stranger, member), db = accounts
    row, identity = resource(db, owner, store)
    signed = signed_storage(monkeypatch, store)
    register(stranger, "stranger@example.com")
    register(member, "member@example.com")
    endpoint = f"/resources/{row.id}/preview-access"
    assert stranger.post(endpoint, json={}).status_code == 404
    share(owner, "member@example.com", "viewer")
    switch(member, identity["workspace_id"])
    before = list(db.scalars(select(UsageEvent)))
    response = member.post(endpoint, json={})
    assert response.status_code == 200, response.text
    grant = response.json()
    assert "no-store" in response.headers["cache-control"]
    assert grant["kind"] == "image" and grant["size_bytes"] == row.size_bytes
    assert grant["headers"] == {"If-Match": store.head(signed[-1][0])["ETag"]}
    assert signed[-1][1:] == (300, True)
    assert member.post(endpoint, json={"revision": grant["revision"]}).json()["revision"] == grant["revision"]
    assert member.post(endpoint, json={"object_key": "other-user"}).status_code == 422
    store.objects[signed[-1][0]] = b"same object updated"
    assert member.post(endpoint, json={"revision": grant["revision"]}).status_code == 409
    assert member.post(endpoint, json={}).json()["revision"] != grant["revision"]
    row.deleted_at = auth.now()
    db.commit()
    assert owner.post(endpoint, json={}).status_code == 404
    assert not list(db.scalars(select(StorageTransfer)))
    assert list(db.scalars(select(UsageEvent))) == before


def test_attachment_capability_does_not_extract_document_and_rejects_deleted_resource(accounts, store, monkeypatch):
    (owner, _, _), db = accounts
    row, identity = resource(db, owner, store, "中文文档.docx", b"not parsed")
    signed = signed_storage(monkeypatch, store)
    attachment = ChatAttachment(workspace_id=row.workspace_id, user_id=UUID(identity["user"]["id"]),
        filename=row.path, content_type=row.content_type, size_bytes=row.size_bytes,
        content_hash=row.content_hash, object_key="attachment-reference", kind="document",
        resource_file_id=row.id)
    db.add(attachment)
    db.commit()
    endpoint = f"/chat/attachments/{attachment.id}/preview-access"
    grant = owner.post(endpoint, json={})
    assert grant.status_code == 200 and grant.json()["kind"] == "docx"
    assert signed[-1][0].endswith(row.path)
    assert attachment.text_content is None
    row.deleted_at = auth.now()
    db.commit()
    assert owner.post(endpoint, json={}).status_code == 404
    assert not list(db.scalars(select(StorageTransfer)))


def test_document_versions_have_distinct_grants_and_download_is_metered(accounts, store, monkeypatch):
    (owner, outsider, _), db = accounts
    register(owner)
    kb = library(owner)
    signed = signed_storage(monkeypatch, store)
    versions = []
    for version in (1, 2):
        key = f"document-v{version}"
        store.objects[key] = b"document-body"
        document = Document(knowledge_base_id=kb, filename="报告.pdf", version_number=version,
            object_key=key, size_bytes=13, content_type="application/pdf", content_hash=key,
            status=DocumentStatus.READY)
        db.add(document)
        versions.append(document)
    db.commit()
    register(outsider, "outsider@example.com")
    for document in versions:
        endpoint = f"/documents/{document.id}/preview-access"
        assert outsider.post(endpoint, json={}).status_code == 404
        assert owner.post(endpoint, json={}).json()["kind"] == "pdf"
        assert signed[-1][0] == document.object_key
    assert not list(db.scalars(select(StorageTransfer)))
    document = versions[0]
    endpoint = f"/documents/{document.id}/download"
    monkeypatch.setattr("app.storage_downloads.download_range_fileobj",
        lambda key, start, end: io.BytesIO(store.objects[key][start:end + 1]))
    assert owner.head(endpoint).status_code == 200
    assert not list(db.scalars(select(StorageTransfer)))
    response = owner.get(endpoint, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == b"docu"
    db.expire_all()
    assert sum(item.sent_bytes for item in db.scalars(select(StorageTransfer))) == 4
    document.status = DocumentStatus.DELETED
    db.commit()
    assert owner.post(f"/documents/{document.id}/preview-access", json={}).status_code == 404


def test_empty_and_legacy_files_have_actionable_types(accounts, store, monkeypatch):
    (owner, _, _), db = accounts
    row, _ = resource(db, owner, store, "legacy.doc", b"")
    signed_storage(monkeypatch, store)
    grant = owner.post(f"/resources/{row.id}/preview-access", json={}).json()
    assert grant["kind"] == "unsupported" and grant["size_bytes"] == 0
    for filename, mime, expected in [
        ("image.svg", "image/svg+xml", "text"), ("page.html", "text/html", "text"),
        ("code.py", "application/octet-stream", "text"), ("deck.pptx", "application/octet-stream", "pptx"),
    ]:
        assert preview.kind(filename, mime) == expected
