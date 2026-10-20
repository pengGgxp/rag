from celery import Celery

from .config import get_settings
from .db import SessionLocal
from .chunking import chunk_text
from .embedding import EmbeddingConfigurationError, OpenAICompatibleEmbeddingProvider
from .models import (
    Chunk,
    Document,
    DocumentStatus,
    EmbeddingStatus,
    IngestionJob,
    JobState,
    KnowledgeBaseStatus,
)
from .parsers import parse_document
from .storage import download_fileobj

celery_app = Celery("rag", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", result_serializer="json")


@celery_app.task(bind=True, autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def process_document(self, job_id: str) -> None:
    session = SessionLocal()
    job = None
    try:
        job = session.get(IngestionJob, job_id)
        if job is None:
            return
        document = session.get(Document, job.document_id)
        if document is None:
            job.state = JobState.FAILED
            job.error_detail = "Document no longer exists"
            session.commit()
            return
        if document.status == DocumentStatus.DELETED:
            job.state = JobState.CANCELLED
            job.error_detail = "Document deleted"
            session.commit()
            return

        job.state = JobState.RUNNING
        document.status = DocumentStatus.PROCESSING
        session.commit()

        content = download_fileobj(document.object_key).read()
        parsed_text, parser_name = parse_document(
            document.filename,
            document.content_type,
            content,
        )
        if not parsed_text.strip():
            raise ValueError("The document parser produced no text")
        session.refresh(document)
        if document.status == DocumentStatus.DELETED:
            job.state = JobState.CANCELLED
            job.error_detail = "Document deleted"
            session.commit()
            return
        document.parsed_text = parsed_text
        document.parser_name = parser_name
        document.chunks.clear()
        document.chunks.extend(
            Chunk(
                ordinal=draft.ordinal,
                text=draft.text,
                token_count=draft.token_count,
                char_start=draft.char_start,
                char_end=draft.char_end,
                heading_path=draft.heading_path,
                content_hash=draft.content_hash,
            )
            for draft in chunk_text(parsed_text)
        )
        if not document.chunks:
            raise ValueError("The document did not produce any chunks")
        document.error_detail = None
        # Parsing and indexing are separate jobs: a document is not READY
        # until every chunk has a successfully stored embedding.
        document.status = DocumentStatus.PROCESSING
        job.state = JobState.SUCCEEDED
        embedding_job = IngestionJob(document_id=document.id)
        session.add(embedding_job)
        session.flush()
        session.refresh(document)
        if document.status == DocumentStatus.DELETED:
            session.rollback()
            job = session.get(IngestionJob, job_id)
            if job is not None:
                job.state = JobState.CANCELLED
                job.error_detail = "Document deleted"
                session.commit()
            return
        session.commit()
        embed_document.delay(str(document.id), str(embedding_job.id))
    except Exception as exc:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        if job is not None:
            job.state = JobState.FAILED
            job.error_detail = str(exc)
            document = session.get(Document, job.document_id)
            if document is not None:
                document.status = DocumentStatus.FAILED
                document.error_detail = str(exc)
                document.knowledge_base.status = KnowledgeBaseStatus.ERROR
            session.commit()
        raise
    finally:
        session.close()


@celery_app.task(bind=True, max_retries=3)
def embed_document(self, document_id: str, job_id: str | None = None) -> None:
    session = SessionLocal()
    try:
        job = session.get(IngestionJob, job_id) if job_id else None
        if job is not None:
            job.state = JobState.RUNNING
            job.error_detail = None
            session.commit()
        document = session.get(Document, document_id)
        if document is None:
            if job is not None:
                job.state = JobState.FAILED
                job.error_detail = "Document no longer exists"
                session.commit()
            return
        if document.status == DocumentStatus.DELETED:
            if job is not None:
                job.state = JobState.CANCELLED
                job.error_detail = "Document deleted"
                session.commit()
            return
        chunks = list(
            session.query(Chunk)
            .filter(Chunk.document_id == document.id)
            .order_by(Chunk.ordinal)
            .all()
        )
        if not chunks:
            raise ValueError("The document has no chunks to embed")

        pending = [
            chunk
            for chunk in chunks
            if chunk.embedding_status in (EmbeddingStatus.PENDING, EmbeddingStatus.FAILED)
        ]
        if not pending:
            # Another worker may currently own RUNNING chunks. Leave the
            # document in PROCESSING instead of falsely reporting readiness.
            if all(chunk.embedding_status == EmbeddingStatus.READY for chunk in chunks):
                document.status = DocumentStatus.READY
                document.error_detail = None
                document.knowledge_base.status = KnowledgeBaseStatus.READY
                if job is not None:
                    job.state = JobState.SUCCEEDED
                session.commit()
            return

        for chunk in pending:
            chunk.embedding_status = EmbeddingStatus.RUNNING
            chunk.embedding_error = None
        document.status = DocumentStatus.PROCESSING
        document.error_detail = None
        session.commit()

        provider = OpenAICompatibleEmbeddingProvider()
        vectors = provider.embed_texts([chunk.text for chunk in pending])
        if len(vectors) != len(pending):
            raise ValueError("Embedding provider returned an unexpected number of vectors")
        session.refresh(document)
        if document.status == DocumentStatus.DELETED:
            if job is not None:
                job.state = JobState.CANCELLED
                job.error_detail = "Document deleted"
                session.commit()
            return
        for chunk, vector in zip(pending, vectors):
            chunk.embedding = vector
            chunk.embedding_status = EmbeddingStatus.READY
            chunk.embedding_error = None
        session.refresh(document)
        if document.status == DocumentStatus.DELETED:
            session.rollback()
            if job is not None:
                job = session.get(IngestionJob, job.id)
                job.state = JobState.CANCELLED
                job.error_detail = "Document deleted"
                session.commit()
            return
        document.status = DocumentStatus.READY
        document.error_detail = None
        document.knowledge_base.status = KnowledgeBaseStatus.READY
        if job is not None:
            job.state = JobState.SUCCEEDED
        session.commit()
    except Exception as exc:
        session.rollback()
        document = session.get(Document, document_id)
        if document is not None:
            chunks = session.query(Chunk).filter(Chunk.document_id == document.id).all()
            for chunk in chunks:
                if chunk.embedding_status == EmbeddingStatus.RUNNING:
                    chunk.embedding_status = EmbeddingStatus.FAILED
                    chunk.embedding_error = str(exc)
            document.status = DocumentStatus.FAILED
            document.error_detail = str(exc)
            document.knowledge_base.status = KnowledgeBaseStatus.ERROR
            if job is not None:
                job.state = JobState.FAILED
                job.error_detail = str(exc)
            session.commit()
        if not isinstance(exc, (ValueError, EmbeddingConfigurationError)) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
        raise
    finally:
        session.close()

