import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import auth, storage_billing
from app.knowledge_workspace import ensure_roots, synchronize
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    EmbeddingStatus,
    IngestionJob,
    JobState,
    KnowledgeBase,
    Membership,
    WorkspaceFile,
)
from app.shared_resources import object_key
from app.workspace_files import resolve_input
from test_auth import library, register, share, switch
from test_cloud_computer_mount import MemoryMount


@pytest.fixture
def workspace_kb(accounts, monkeypatch):
    (client, teammate, _), db = accounts
    identity = register(client)
    knowledge_base_id = library(client)
    access = auth.Access(
        UUID(identity["user"]["id"]),
        UUID(identity["workspace_id"]),
        "admin",
        auth.digest(client.cookies.get(auth.COOKIE)),
    )
    store = MemoryMount()
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: store)
    monkeypatch.setattr(storage_billing, "check_capacity", lambda *_args, **_kwargs: None)
    row = db.get(KnowledgeBase, knowledge_base_id)
    ensure_roots(db, access.workspace_id, access)
    return client, teammate, db, access, row, store


def put(store, workspace_id, path, content):
    store.objects[object_key(workspace_id, path)] = content


def test_new_file_move_and_replacement_follow_versioned_ingestion(workspace_kb, monkeypatch):
    _, _, db, access, library_row, store = workspace_kb
    root = library_row.workspace_path
    path = f"{root}/guide.txt"
    original = b"first version"
    put(store, access.workspace_id, path, original)
    dispatched = []

    first = synchronize(db, access, uuid4(), dispatch=dispatched.append, knowledge_base_id=library_row.id)
    document = db.scalar(select(Document).where(Document.workspace_path == path))
    first_job = db.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id))
    assert first["counts"]["added"] == 1
    assert document.status == DocumentStatus.QUEUED
    assert dispatched == [str(first_job.id)]

    # A move keeps the same document and its existing ingestion job.
    moved_path = f"{root}/manual/guide.txt"
    from app.workspace_mutations import create_directory, transfer
    create_directory(db, access, f"{root}/manual", request_id=str(uuid4()))
    moved = transfer(db, access, [path], destination=f"{root}/manual", move=True, request_id=str(uuid4()))
    assert moved["knowledge_base_sync"]["counts"]["moved"] == 1
    assert document.workspace_path == moved_path
    assert db.scalar(select(IngestionJob.id).where(IngestionJob.document_id == document.id)) == first_job.id
    assert dispatched == [str(first_job.id)]

    # Preserve the pre-write bytes and create a new, inactive version.
    request_id = str(uuid4())
    replacement = b"replacement version"
    from app import tasks
    from app.file_operations import file_revision
    from app.workspace_mutations import save_files
    actual_dispatched = []
    monkeypatch.setattr(tasks.process_document, "delay", actual_dispatched.append)
    resource = db.scalar(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.path == moved_path))
    saved = save_files(db, access, {moved_path: (replacement, "text/plain")},
        expected={moved_path: file_revision(resource)}, request_id=request_id)
    updated = saved["knowledge_base_sync"]
    versions = db.scalars(select(Document).where(
        Document.version_group_id == document.version_group_id
    ).order_by(Document.version_number)).all()
    assert updated["counts"]["updated"] == 1
    assert len(versions) == 2
    assert versions[0].object_key.startswith(f"workspaces/{access.workspace_id}/knowledge-history/")
    assert store.objects[versions[0].object_key] == original
    assert versions[0].is_active
    assert not versions[1].is_active and versions[1].status == DocumentStatus.QUEUED
    assert actual_dispatched == [str(db.scalar(select(IngestionJob.id).where(
        IngestionJob.document_id == versions[1].id)))]


def test_delete_stops_indexing_and_restore_reuses_parsed_chunks(workspace_kb):
    _, _, db, access, library_row, store = workspace_kb
    path = f"{library_row.workspace_path}/restore.txt"
    content = b"retained source"
    put(store, access.workspace_id, path, content)
    synchronize(db, access, uuid4(), dispatch=lambda _job: None, knowledge_base_id=library_row.id)
    document = db.scalar(select(Document).where(Document.workspace_path == path))
    document.parsed_text = "retained parsed text"
    document.chunks.append(Chunk(
        ordinal=0,
        text="retained parsed text",
        token_count=3,
        char_start=0,
        char_end=20,
        content_hash=hashlib.sha256(b"retained parsed text").hexdigest(),
        embedding_status=EmbeddingStatus.PENDING,
    ))
    db.commit()

    from app.knowledge_workspace import prepare_changes
    request_id = uuid4()
    prepare_changes(db, access, request_id, paths=[path])
    store.delete(object_key(access.workspace_id, path))
    deleted = synchronize(db, access, request_id, dispatch=lambda _job: None, knowledge_base_id=library_row.id)
    assert deleted["counts"]["deleted"] == 1
    assert document.status == DocumentStatus.DELETED and not document.is_active
    assert document.object_key.startswith(f"workspaces/{access.workspace_id}/knowledge-history/")

    put(store, access.workspace_id, path, content)
    dispatched = []
    restored = synchronize(db, access, uuid4(), dispatch=dispatched.append, knowledge_base_id=library_row.id)
    db.refresh(document)
    assert restored["counts"]["updated"] == 1
    assert document.status == DocumentStatus.QUEUED and document.is_active
    assert document.parsed_text == "retained parsed text"
    assert len(document.chunks) == 1 and document.chunks[0].embedding_status == EmbeddingStatus.PENDING
    job = db.scalar(select(IngestionJob).where(
        IngestionJob.document_id == document.id,
        IngestionJob.state == JobState.QUEUED,
    ))
    assert job is not None and str(job.id) in dispatched


def test_viewer_cannot_synchronize_knowledge_files(workspace_kb):
    _, _, db, access, library_row, store = workspace_kb
    membership = db.get(Membership, (access.workspace_id, access.user_id))
    membership.role = "viewer"
    db.commit()
    path = f"{library_row.workspace_path}/forbidden.txt"
    put(store, access.workspace_id, path, b"must not be admitted")
    from app.knowledge_workspace import synchronize
    with pytest.raises(HTTPException) as error:
        synchronize(db, access, uuid4(), knowledge_base_id=library_row.id)
    assert error.value.status_code == 403
    assert db.scalar(select(Document.id).where(Document.workspace_path == path)) is None


def test_knowledge_upload_writes_the_shared_original_and_queues_once(workspace_kb, monkeypatch):
    client, _, db, access, library_row, store = workspace_kb
    from app import main
    dispatched = []
    monkeypatch.setattr(main.process_document, "delay", dispatched.append)
    content = b"same bytes for workspace and knowledge base"

    response = client.post(
        f"/knowledge-bases/{library_row.id}/documents",
        files={"file": ("guide.txt", content, "text/plain")},
    )

    assert response.status_code == 201, response.text
    document = db.get(Document, UUID(response.json()["id"]))
    assert document.workspace_path == f"{library_row.workspace_path}/guide.txt"
    assert document.object_key == object_key(access.workspace_id, document.workspace_path)
    assert document.workspace_file_id is not None
    assert store.objects[document.object_key] == content
    job = db.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id))
    assert dispatched == [str(job.id)]
    files = client.get("/resources", params={"q": "guide.txt"}).json()["items"]
    assert len(files) == 1 and files[0]["path"] == document.workspace_path
    assert files[0]["id"] == str(document.workspace_file_id)


def test_dispatch_resumes_at_embedding_when_chunks_are_already_parsed(monkeypatch):
    from types import SimpleNamespace
    from app import tasks
    from app.document_ingestion import dispatch_document_job
    parsed = []
    embedded = []
    monkeypatch.setattr(tasks.process_document, "delay", parsed.append)
    monkeypatch.setattr(tasks.embed_document, "delay", lambda document_id, job_id: embedded.append((document_id, job_id)))
    job_id, document_id = uuid4(), uuid4()

    dispatch_document_job(SimpleNamespace(id=job_id, document=SimpleNamespace(
        id=document_id, parsed_text="parsed text", chunks=[object()])))
    dispatch_document_job(SimpleNamespace(id=job_id, document=SimpleNamespace(
        id=document_id, parsed_text=None, chunks=[])))

    assert embedded == [(str(document_id), str(job_id))]
    assert parsed == [str(job_id)]


def test_workspace_editor_can_read_original_from_a_restricted_library(workspace_kb):
    owner, teammate, db, owner_access, library_row, store = workspace_kb
    library_row.restricted = True
    db.commit()
    identity = register(teammate, "editor@example.com", "Editor")
    share(owner, "editor@example.com", "editor")
    switch(teammate, str(owner_access.workspace_id))
    editor_access = auth.Access(
        UUID(identity["user"]["id"]),
        owner_access.workspace_id,
        "editor",
        auth.digest(teammate.cookies.get(auth.COOKIE)),
    )
    path = f"{library_row.workspace_path}/shared.txt"
    content = b"workspace-shared knowledge original"
    put(store, owner_access.workspace_id, path, content)

    result = synchronize(db, editor_access, uuid4(), dispatch=lambda _job: None)

    assert result["counts"]["added"] == 1
    resource = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == owner_access.workspace_id,
        WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))
    row, _ = resolve_input(db, editor_access, {"id": str(resource.id)})
    assert row.path == path
    with store.download(object_key(owner_access.workspace_id, path)) as source:
        assert source.read() == content
