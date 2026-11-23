import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, file_preview_access, tasks
from app.knowledge import routes as knowledge_routes
from app.knowledge_originals import original_key, prefix, resolve_citation
from app.migrate_knowledge_originals import migrate
from app.models import ChatMessage, Chunk, Conversation, Document, DocumentStatus, EmbeddingStatus, IngestionJob, JobState, KnowledgeBase
from app.storage_billing import inventory
from test_auth import library, register
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_upload_replace_delete_restore_and_historical_citation(accounts, monkeypatch):
    (owner, outsider, _), db = accounts
    owner_id = UUID(register(owner)["user"]["id"])
    register(outsider, "outsider@example.com")
    kb = library(owner)
    library(outsider)
    store = MemoryMount()
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    monkeypatch.setattr(file_preview_access, "current_storage", lambda _: store)
    queued = []
    monkeypatch.setattr(knowledge_routes.process_document, "delay", queued.append)
    endpoint = f"/knowledge-bases/{kb}/documents"
    a, b = b"original orchid text", b"updated cactus text"

    def upload(content, previous=None, digest=None, request_id=None):
        return owner.post(endpoint, files={"file": ("notes.txt", content, "text/plain")},
            data={"replace_document_id": str(previous), "expected_content_hash": digest} if previous else {},
            headers={"Idempotency-Key": str(request_id or uuid4())})

    created = upload(a)
    assert created.status_code == 201, created.text
    identifier = UUID(created.json()["id"])
    key = original_key(owner_id, kb, identifier, "notes.txt")
    assert store.objects == {key: a}
    assert inventory(db, owner_id)["breakdown"]["documents"] == len(a)
    assert outsider.post(endpoint, files={"file": ("notes.txt", b)}).status_code == 404
    first_job = UUID(queued[0])
    document = db.get(Document, identifier)
    document.status = DocumentStatus.READY
    db.get(IngestionJob, first_job).state = JobState.SUCCEEDED
    db.get(IngestionJob, first_job).phase = "indexing"
    chunk = Chunk(ordinal=0, text=a.decode(), token_count=3, char_start=0, char_end=len(a),
        content_hash=sha(a), embedding=[1.0] + [0.0] * 1023, embedding_status=EmbeddingStatus.READY)
    document.chunks.append(chunk)
    db.commit()
    conv = owner.post("/conversations", json={}).json()["id"]
    message = ChatMessage(conversation_id=UUID(conv), sequence=1, role="assistant", status="completed",
        citations=[{"chunk_id": str(chunk.id), "document_id": str(identifier), "original_hash": sha(a),
                    "page_number": 2, "text": a.decode()}])
    db.add(message)
    db.commit()
    citation = f"/conversations/{conv}/messages/{message.id}/citations/{chunk.id}"
    preview = f"/documents/{identifier}/preview-access"
    grant = owner.post(preview, json={}).json()
    assert owner.get(grant["url"], headers={**grant["headers"], "Range": "bytes=0-2"}).content == a[:3]
    assert outsider.get(grant["url"], headers=grant["headers"]).status_code == 404
    assert owner.get(citation).json()["url"].endswith("&page=2")
    assert outsider.get(citation).status_code == 404
    assert owner.delete(f"/documents/{identifier}").status_code == 200
    assert key not in store.objects
    assert owner.get(grant["url"], headers=grant["headers"]).status_code == 404
    assert owner.get(citation).json()["state"] == "unavailable"
    restored = owner.post(f"/documents/{identifier}/restore")
    assert restored.json()["searchable"] and len(queued) == 1
    assert store.objects[key] == a
    noop = uuid4()
    assert upload(a, identifier, sha(a), noop).json()["searchable"]
    assert len(queued) == 1
    request_id = uuid4()
    assert upload(b, identifier, sha(a), request_id).json()["id"] == str(identifier)
    stale_job = UUID(queued[-1])
    assert upload(b, identifier, sha(a), request_id).json()["id"] == str(identifier)
    assert len(queued) == 2
    assert owner.get(citation).json()["state"] == "changed"
    assert owner.get(grant["url"], headers=grant["headers"]).status_code == 409
    assert owner.post(f"/knowledge-bases/{kb}/search", params={"query": "orchid"}).json() == []
    assert upload(a, identifier, sha(b)).status_code == 201
    assert upload(a, identifier, sha(a), noop).status_code == 201
    assert upload(b, identifier, sha(a)).status_code == 201
    assert len(queued) == 4
    with Session(db.get_bind()) as worker:
        tasks._process_document(tasks.process_document, worker, stale_job, b)
    db.expire_all()
    assert db.get(IngestionJob, stale_job).state == JobState.CANCELLED
    assert owner.post(f"/jobs/{stale_job}/retry").status_code == 404
    assert db.get(Document, identifier).chunks == []
    assert store.objects == {key: b}
    assert owner.get("/resources").json()["items"] == []


def test_migration_replays_and_preserves_current_vectors_and_legacy_citations(auth_postgres):
    store = MemoryMount()
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"migration-{uuid4().hex}@example.com", "Migration")
        kb = KnowledgeBase(account_id=user.id, owner_id=user.id, name="Migration test")
        db.add(kb)
        db.flush()
        group = uuid4()

        def document(content, *, active=True, group_id=None, deleted=False, put=True):
            identifier = uuid4()
            row = Document(id=identifier, knowledge_base_id=kb.id, version_group_id=group_id or identifier,
                object_key=f"{kb.id}/{identifier}.txt", content_hash=sha(content), filename="same.txt",
                size_bytes=len(content), content_type="text/plain", is_active=active,
                status=DocumentStatus.DELETED if deleted else DocumentStatus.READY, parsed_text=content.decode())
            db.add(row)
            if put:
                store.objects[row.object_key] = content
            return row

        old = document(b"old", active=False, group_id=group)
        current = document(b"current", group_id=group)
        deleted = document(b"recycle", active=False, deleted=True)
        missing = document(b"missing", put=False)
        for row in (old, current):
            row.chunks.append(Chunk(ordinal=0, text=row.parsed_text, token_count=1, char_start=0,
                char_end=row.size_bytes, content_hash=row.content_hash, embedding=[1.0] + [0.0] * 1023,
                embedding_status=EmbeddingStatus.READY))
        db.commit()
        before = inventory(db, user.id)
        previous_paths = {row.object_key for row in (old, current, deleted)}
        old_evidence = {"version_id": str(old.id), "text": old.parsed_text}
        old_chunk = old.chunks[0].id
        current_chunk = current.chunks[0].id
        conversation = Conversation(account_id=user.id, owner_id=user.id)
        conversation.messages.append(ChatMessage(sequence=1, role="assistant", citations=[
            {**old_evidence, "chunk_id": str(old_chunk)},
            {"version_id": str(current.id), "chunk_id": str(current_chunk), "text": current.parsed_text},
        ]))
        db.add(conversation)
        db.commit()
        draft = migrate(db, store)
        assert draft["issues"] == [{"group_id": str(missing.version_group_id), "library_id": str(kb.id),
            "documents": [str(missing.id)], "reason": "missing_original"}]
        assert not any(key.startswith(prefix(user.id)) for key in store.objects)
        first = migrate(db, store, execute=True, offline=True)
        second = migrate(db, store, execute=True, offline=True)
        assert first["issues"] == second["issues"] == draft["issues"]
        assert first["current_vectors"] == second["current_vectors"] == 1
        assert first["current_originals"] == second["current_originals"] == 2
        assert first["citations_before"] == first["citations_after"] == {
            str(user.id): {"current": 1, "changed": 1}}
        assert first["document_bytes_after"][str(user.id)] == (
            first["document_bytes_before"][str(user.id)] + deleted.size_bytes)
        assert conversation.messages[0].citations[1]["original_hash"] == current.content_hash
        assert previous_paths <= set(store.objects)
        assert store.objects[current.object_key] == b"current"
        assert store.objects[old.object_key] == b"old" and "/knowledge-migration-backup/" in old.object_key
        assert store.objects[deleted.object_key] == b"recycle" and "/knowledge-trash/" in deleted.object_key
        assert deleted.is_active and db.get(Chunk, current_chunk).embedding is not None
        assert db.get(Chunk, old_chunk) is None
        access = auth.Access(user.id, "test")
        assert resolve_citation(db, access, old_evidence, old_chunk)["state"] == "changed"
        assert resolve_citation(db, access, {"version_id": str(current.id), "text": current.parsed_text},
            current_chunk)["state"] == "current"
        assert inventory(db, user.id)["used_bytes"] == before["used_bytes"] + deleted.size_bytes
