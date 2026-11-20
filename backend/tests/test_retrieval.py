from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.embedding import EmbeddingProviderError
from app.models import (
    Chunk,
    Document,
    DocumentStatus,
    EmbeddingStatus,
    KnowledgeBase,
    QueryTrace,
    User,
)
from auth_helpers import authenticate


@pytest.fixture
def retrieval_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    monkeypatch.setattr(
        main,
        "OpenAICompatibleEmbeddingProvider",
        lambda: SimpleNamespace(
            status=SimpleNamespace(
                configured=False,
                provider="openai-compatible",
                model=None,
                dimension=1024,
            )
        ),
    )
    main.app.dependency_overrides[main.get_db] = lambda: session
    client = TestClient(main.app)
    authenticate(client, session)
    try:
        yield client, session
    finally:
        client.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        session.close()
        engine.dispose()


def add_document(session, library, text, *, status=DocumentStatus.READY):
    document = Document(
        knowledge_base_id=library.id,
        filename=f"{uuid4()}.txt",
        content_type="text/plain",
        size_bytes=len(text.encode()),
        object_key=str(uuid4()),
        content_hash=str(uuid4()),
        status=status,
    )
    document.chunks.append(
        Chunk(
            ordinal=0,
            text=text,
            token_count=len(text),
            char_start=0,
            char_end=len(text),
            content_hash=str(uuid4()),
            embedding_status=EmbeddingStatus.READY,
        )
    )
    session.add(document)
    session.commit()
    return document


def add_library(session):
    user_id = session.scalar(select(User.id).where(User.email == "owner@example.com"))
    library = KnowledgeBase(account_id=user_id, owner_id=user_id, name="retrieval test")
    session.add(library)
    session.commit()
    return library


def test_retrieval_isolates_library_and_excludes_unavailable_documents(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    other = add_library(session)
    first = add_document(session, library, "needle needle needle")
    second = add_document(session, library, "needle")
    add_document(session, other, "needle needle needle needle")
    for status in (
        DocumentStatus.DELETED,
        DocumentStatus.PROCESSING,
        DocumentStatus.FAILED,
        DocumentStatus.QUEUED,
    ):
        add_document(session, library, "needle needle", status=status)

    response = client.post(
        f"/knowledge-bases/{library.id}/retrieval", json={"query": "  needle  "}
    )

    assert response.status_code == 200
    payload = response.json()
    assert [row["chunk"]["document_id"] for row in payload["results"]] == [
        str(first.id), str(second.id)
    ]
    trace = payload["trace"]
    assert trace["query"] == "needle"
    assert trace["knowledge_base_id"] == str(library.id)
    assert trace["retrieval_mode"] == "bm25"
    assert trace["result_count"] == 2
    assert [row["lexical_rank"] for row in trace["results"]] == [1, 2]
    assert trace["results"][0]["score"] == pytest.approx(1 / 61)
    assert trace["results"][0]["bm25_score"] > 0
    assert all(row["vector_rank"] is None for row in trace["results"])
    assert {row["trace_id"] for row in payload["results"]} == {trace["id"]}
    persisted = client.get(f"/traces/{trace['id']}")
    assert persisted.json() == trace
    assert payload["results"][0]["chunk"]["char_end"] == len(first.chunks[0].text)


def test_retrieval_can_search_a_user_mentioned_document(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    selected = add_document(session, library, "needle selected evidence")
    other = add_document(session, library, "needle other evidence")

    response = client.post(
        f"/knowledge-bases/{library.id}/retrieval",
        json={"query": "needle", "document_ids": [str(selected.id)]},
    )

    assert response.status_code == 200
    assert [row["chunk"]["document_id"] for row in response.json()["results"]] == [str(selected.id)]
    assert all(row["chunk"]["document_id"] != str(other.id) for row in response.json()["results"])


def test_document_filter_cannot_escape_library_or_active_version(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    other_library = add_library(session)
    active = add_document(session, library, "needle active")
    inactive = add_document(session, library, "needle inactive")
    inactive.is_active = False
    outside = add_document(session, other_library, "needle outside")
    deleted = add_document(session, library, "needle deleted", status=DocumentStatus.DELETED)
    session.commit()
    response = client.post(f"/knowledge-bases/{library.id}/retrieval", json={"query": "needle",
        "document_ids": [str(row.id) for row in (active, inactive, outside, deleted)]})
    assert response.status_code == 200
    assert [row["chunk"]["document_id"] for row in response.json()["results"]] == [str(active.id)]


def test_document_versions_preserve_history_and_only_active_version_is_retrieved(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    first = add_document(session, library, "old release guidance")
    first.version_group_id = first.id
    first.version_number = 1
    first.is_active = True
    second = Document(
        knowledge_base_id=library.id,
        version_group_id=first.id,
        version_number=2,
        is_active=False,
        supersedes_id=first.id,
        filename=first.filename,
        content_type="text/plain",
        size_bytes=19,
        object_key=str(uuid4()),
        content_hash=str(uuid4()),
        status=DocumentStatus.READY,
    )
    second.chunks.append(Chunk(
        ordinal=0, text="new release guidance", token_count=3, char_start=0, char_end=21,
        content_hash=str(uuid4()), embedding_status=EmbeddingStatus.READY,
    ))
    session.add(second)
    session.commit()

    results = client.post(f"/knowledge-bases/{library.id}/search", params={"query": "release"}).json()
    assert [row["chunk"]["text"] for row in results] == ["old release guidance"]
    versions = client.get(f"/documents/{first.id}/versions")
    assert versions.status_code == 200
    assert [row["version_number"] for row in versions.json()] == [2, 1]
    assert client.post(f"/documents/{second.id}/activate").status_code == 200
    results = client.post(f"/knowledge-bases/{library.id}/search", params={"query": "release"}).json()
    assert [row["chunk"]["text"] for row in results] == ["new release guidance"]
def test_empty_retrieval_returns_a_unique_persisted_trace(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    responses = [
        client.post(f"/knowledge-bases/{library.id}/retrieval", json={"query": "missing"})
        .json()
        for _ in range(2)
    ]
    assert responses[0]["trace"]["id"] != responses[1]["trace"]["id"]
    for payload in responses:
        assert payload["results"] == []
        assert payload["trace"]["result_count"] == 0
        assert client.get(f"/traces/{payload['trace']['id']}").status_code == 200
    assert len(session.scalars(select(QueryTrace)).all()) == 2


@pytest.mark.parametrize("query", ["", "   ", "x" * 4001])
def test_inspector_rejects_invalid_query(retrieval_client, query):
    client, session = retrieval_client
    library = add_library(session)
    response = client.post(
        f"/knowledge-bases/{library.id}/retrieval", json={"query": query}
    )
    assert response.status_code == 422
    assert session.scalars(select(QueryTrace)).all() == []


def test_missing_library_returns_not_found(retrieval_client):
    client, _session = retrieval_client
    response = client.post(
        f"/knowledge-bases/{uuid4()}/retrieval", json={"query": "needle"}
    )
    assert response.status_code == 404


def test_legacy_search_response_stays_a_list(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    document = add_document(session, library, "needle")
    response = client.post(
        f"/knowledge-bases/{library.id}/search", params={"query": "needle"}
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["chunk"]["document_id"] == str(document.id)


def test_vector_failure_is_visible_as_bm25_fallback(retrieval_client, monkeypatch):
    client, session = retrieval_client
    library = add_library(session)
    add_document(session, library, "needle")

    def fail_embedding(_texts):
        raise EmbeddingProviderError("upstream unavailable")

    monkeypatch.setattr(
        main,
        "OpenAICompatibleEmbeddingProvider",
        lambda: SimpleNamespace(
            status=SimpleNamespace(
                configured=True,
                provider="openai-compatible",
                model="test-embedding",
                dimension=1024,
            ),
            embed_texts=fail_embedding,
        ),
    )
    response = client.post(
        f"/knowledge-bases/{library.id}/retrieval", json={"query": "needle"}
    )
    assert response.status_code == 200
    assert response.json()["trace"]["retrieval_mode"] == "bm25_fallback"
    assert response.json()["trace"]["embedding_dimension"] == 1024
    assert len(response.json()["results"]) == 1


def test_no_evidence_answer_keeps_its_own_trace(retrieval_client):
    client, session = retrieval_client
    library = add_library(session)
    first = client.post(
        f"/knowledge-bases/{library.id}/answer", json={"query": "missing", "provider_id": str(uuid4())}
    )
    second = client.post(
        f"/knowledge-bases/{library.id}/answer", json={"query": "missing", "provider_id": str(uuid4())}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["trace_id"] != second.json()["trace_id"]
    for response in (first, second):
        assert response.json()["refused"] is True
        assert response.json()["citations"] == []
        trace = client.get(f"/traces/{response.json()['trace_id']}")
        assert trace.json()["retrieval_mode"] == "no_evidence"
