from celery import Celery
from sqlalchemy import select
from uuid import UUID

from .config import get_settings
from .db import SessionLocal
from .docmind import AliyunDocMind, DocMindError, PARSER_NAME, ParsingCancelled
from .parsed_document import ParsedDocument, parsed_chunks
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
from .parsers import parse_document, parse_local_pdf, pdf_pages
from .storage import download_fileobj

celery_app = Celery("rag", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", result_serializer="json")


def parse_for_ingestion(session, job, document, content: bytes) -> ParsedDocument:
    if not (document.filename.lower().endswith(".pdf") or document.content_type == "application/pdf"):
        text, name = parse_document(document.filename, document.content_type, content)
        return ParsedDocument(text, name)
    settings = get_settings()
    metadata = document.parsing_metadata or {}
    if metadata.get("status") == "complete" and document.parsed_text:
        return ParsedDocument(document.parsed_text, document.parser_name, metadata["pages"])
    if settings.pdf_parser == "local" and not metadata.get("task_id"):
        return parse_local_pdf(content)

    page_count = len(pdf_pages(content))
    if page_count > settings.docmind_max_pages:
        raise DocMindError(f"PDF 超过云端解析页数限制（{settings.docmind_max_pages} 页），未提交云端")
    provider = AliyunDocMind(settings)

    def check_active():
        session.refresh(job)
        session.refresh(document)
        if job.state != JobState.RUNNING or document.status == DocumentStatus.DELETED:
            raise ParsingCancelled()

    check_active()
    if not metadata.get("task_id"):
        if metadata.get("status") == "submitting":
            raise DocMindError("上次云端提交结果未知，已阻止重复付费提交，请由管理员核对云端任务")
        metadata = {
            "provider": PARSER_NAME, "status": "submitting", "page_count": page_count,
            "config": {"llm_enhancement": False, "formula_enhancement": False,
                       "output_format": ["markdown", "visualLayoutInfo"]},
        }
        document.parsing_metadata = metadata
        session.commit()
        try:
            task_id = provider.submit(content, page_count)
        except DocMindError as exc:
            if not exc.uncertain:
                # An explicit rejection is safe to retry after configuration is fixed.
                document.parsing_metadata = None
                session.commit()
            raise
        document.parsing_metadata = {**metadata, "status": "processing", "task_id": task_id}
        session.commit()
        metadata = document.parsing_metadata
    parsed = provider.collect(metadata["task_id"], page_count, check_active)
    check_active()
    document.parsed_text = parsed.text
    document.parser_name = parsed.parser_name
    document.parsing_metadata = {**metadata, "status": "complete", "pages": parsed.pages}
    session.commit()
    return parsed


@celery_app.task(bind=True, autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def process_document(self, job_id: str) -> None:
    job_id = UUID(str(job_id))
    session = SessionLocal()
    job = None
    try:
        job = session.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
        if job is None:
            return
        document = session.get(Document, job.document_id)
        if document is None:
            job.state = JobState.FAILED
            job.error_detail = "Document no longer exists"
            session.commit()
            return
        if job.state in (JobState.CANCELLED, JobState.SUCCEEDED, JobState.RUNNING):
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
        parsed = parse_for_ingestion(session, job, document, content)
        if not parsed.text.strip():
            raise ValueError("The document parser produced no text")
        session.refresh(document)
        session.refresh(job)
        if job.state == JobState.CANCELLED:
            return
        if document.status == DocumentStatus.DELETED:
            job.state = JobState.CANCELLED
            job.error_detail = "Document deleted"
            session.commit()
            return
        document.parsed_text = parsed.text
        document.parser_name = parsed.parser_name
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
                source_locations=locations,
            )
            for draft, locations in parsed_chunks(parsed)
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
    except ParsingCancelled:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        if job is not None and job.state == JobState.RUNNING:
            job.state = JobState.CANCELLED
            job.error_detail = "Document deleted or processing cancelled"
            session.commit()
    except Exception as exc:
        session.rollback()
        job = session.get(IngestionJob, job_id)
        if job is not None:
            document = session.get(Document, job.document_id)
            if job.state == JobState.CANCELLED or (document and document.status == DocumentStatus.DELETED):
                if job.state != JobState.CANCELLED:
                    job.state = JobState.CANCELLED
                    session.commit()
                return
            if job.state == JobState.SUCCEEDED:
                # Broker delivery may fail after parsing was committed.
                raise
            job.state = JobState.FAILED
            job.error_detail = str(exc)
            document = session.get(Document, job.document_id)
            if document is not None:
                document.status = DocumentStatus.FAILED
                document.error_detail = str(exc)
                if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
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
        if job is not None and job.state == JobState.CANCELLED:
            return
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
                if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
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
        if job is not None:
            session.refresh(job)
            if job.state == JobState.CANCELLED:
                return
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
        if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
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
            if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
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

