import math
import re
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..identity.auth import Access
from .ingestion.embedding import EmbeddingProviderError, OpenAICompatibleEmbeddingProvider
from ..models import Chunk, Document, DocumentStatus, KnowledgeBase, KnowledgeBaseStatus, QueryTrace
from .schemas import ChunkRead, QueryTraceRead, RetrievalRead, SearchResult


def retrieve_knowledge_base(
    knowledge_base_id: UUID, query: str, db: Session, access: Access,
    *, result_limit: int = 20, document_ids: list[UUID] | None = None,
) -> RetrievalRead:
    access.library(db, knowledge_base_id)
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base and knowledge_base.status == KnowledgeBaseStatus.ARCHIVED:
        raise HTTPException(409, "知识库已归档，无法检索")
    terms = [term for term in re.findall(r"\w+", query.casefold()) if term]
    trace_id = uuid4()
    lexical_rows = []
    rows = []
    corpus_rows = []
    if terms:
        lexical_scope = (
            Document.knowledge_base_id == knowledge_base_id,
            Document.status == DocumentStatus.READY,
            Document.is_active.is_(True),
        )
        if document_ids is not None:
            lexical_scope += (Document.id.in_(document_ids),)
        corpus_rows = db.execute(
            select(Chunk.text, Chunk.token_count)
            .join(Document, Document.id == Chunk.document_id)
            .where(*lexical_scope)
        ).all()
        if db.get_bind().dialect.name == "postgresql":
            document_vector = func.to_tsvector("simple", Chunk.text)
            query_vector = func.websearch_to_tsquery("simple", " OR ".join(terms))
            rows = db.execute(
                select(Chunk, Document.filename)
                .join(Document, Document.id == Chunk.document_id)
                .where(*lexical_scope, document_vector.op("@@")(query_vector))
            ).all()
        else:
            # SQLite is used by the unit-test harness only. Production uses
            # PostgreSQL's indexed full-text path above.
            rows = db.execute(
                select(Chunk, Document.filename)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    *lexical_scope,
                    or_(*[Chunk.text.ilike(f"%{term}%") for term in terms]),
                )
            ).all()
    if rows:
        corpus_size = max(len(corpus_rows), 1)
        average_length = sum(
            max(token_count, 1) for _, token_count in corpus_rows
        ) / corpus_size
        document_frequency = {
            term: sum(
                1 for text_value, _ in corpus_rows if term in text_value.casefold()
            )
            for term in terms
        }
        for chunk, filename in rows:
            tokens = re.findall(r"\w+", chunk.text.casefold())
            score = 0.0
            length = max(chunk.token_count, 1)
            for term in terms:
                frequency = tokens.count(term)
                if not frequency:
                    continue
                df = document_frequency[term]
                inverse_frequency = math.log(
                    1.0 + (corpus_size - df + 0.5) / (df + 0.5)
                )
                saturation = (
                    frequency * 2.0
                    / (
                        frequency
                        + 1.2 * (0.25 + 0.75 * length / max(average_length, 1.0))
                    )
                )
                score += inverse_frequency * saturation
            lexical_rows.append((chunk, filename, score))
    lexical_rows.sort(key=lambda item: item[2], reverse=True)

    vector_rows = []
    retrieval_mode = "bm25"
    provider = OpenAICompatibleEmbeddingProvider()
    if terms and provider.status.configured:
        try:
            query_vector = provider.embed_texts([query])[0]
            vector_rows = db.execute(
                select(
                    Chunk,
                    Document.filename,
                    Chunk.embedding.cosine_distance(query_vector).label("vector_distance"),
                )
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.status == DocumentStatus.READY,
                    Document.is_active.is_(True),
                    Chunk.embedding.is_not(None),
                    *([Document.id.in_(document_ids)] if document_ids is not None else []),
                )
                .order_by(Chunk.embedding.cosine_distance(query_vector))
                .limit(result_limit)
            ).all()
            retrieval_mode = "hybrid"
        except (EmbeddingProviderError, IndexError):
            vector_rows = []
            retrieval_mode = "bm25_fallback"

    # Reciprocal rank fusion keeps lexical search useful for exact terms while
    # allowing semantic matches to rise when embeddings are available.
    fused: dict[UUID, dict] = {}
    lexical_ranks: dict[UUID, int] = {}
    for rank, (chunk, filename, _lexical_score) in enumerate(lexical_rows, start=1):
        lexical_ranks[chunk.id] = rank
        fused[chunk.id] = {
            "chunk": chunk,
            "filename": filename,
            "score": 1.0 / (60 + rank),
            "bm25_score": float(_lexical_score),
        }
    vector_ranks: dict[UUID, int] = {}
    vector_distances: dict[UUID, float] = {}
    for rank, (chunk, filename, vector_distance) in enumerate(vector_rows, start=1):
        vector_ranks[chunk.id] = rank
        vector_distances[chunk.id] = float(vector_distance)
        entry = fused.setdefault(
            chunk.id,
            {"chunk": chunk, "filename": filename, "score": 0.0, "bm25_score": None},
        )
        entry["vector_distance"] = float(vector_distance)
        entry["score"] += 1.0 / (60 + rank)

    ordered_results = sorted(
        fused.items(), key=lambda item: item[1]["score"], reverse=True,
    )[:result_limit]
    trace_results = [
        {
            "chunk_id": str(chunk_id),
            "filename": entry["filename"],
            "score": float(entry["score"]),
            "bm25_score": entry.get("bm25_score"),
            "rank": rank,
            "lexical_rank": lexical_ranks.get(chunk_id),
            "vector_rank": vector_ranks.get(chunk_id),
            "vector_distance": vector_distances.get(chunk_id),
            "version_group_id": str(entry["chunk"].document.version_group_id) if entry["chunk"].document.version_group_id else None,
            "version_number": entry["chunk"].document.version_number,
        }
        for rank, (chunk_id, entry) in enumerate(ordered_results, start=1)
    ]
    trace = QueryTrace(
        id=trace_id,
        owner_id=access.user_id,
        knowledge_base_id=knowledge_base_id,
        query=query,
        retrieval_mode=retrieval_mode,
        embedding_provider=provider.status.provider,
        embedding_model=provider.status.model,
        embedding_dimension=provider.status.dimension if provider.status.configured else None,
        result_count=len(trace_results),
        results=trace_results,
    )
    db.add(trace)
    db.commit()
    db.refresh(trace)
    db.expire_all()
    access.revalidate(db).library(db, knowledge_base_id)

    current = dict(db.execute(select(Chunk.id, Document.content_hash).join(Document).where(
        Chunk.id.in_([identifier for identifier, _ in ordered_results]),
        Document.status == DocumentStatus.READY, Document.is_active.is_(True),
    )).all())
    results = [
        SearchResult(
            trace_id=trace_id,
            chunk=ChunkRead.model_validate(entry["chunk"]).model_copy(update={
                "version_group_id": entry["chunk"].document.version_group_id,
                "version_number": entry["chunk"].document.version_number,
                "content_origin": entry["chunk"].document.content_origin,
                "original_hash": current[identifier],
            }),
            score=float(entry["score"]),
            filename=entry["filename"],
        )
        for identifier, entry in ordered_results if identifier in current
    ]
    return RetrievalRead(trace=QueryTraceRead.model_validate(trace), results=results)
