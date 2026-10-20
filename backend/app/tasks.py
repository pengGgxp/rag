from celery import Celery

from .config import get_settings
from .db import SessionLocal
from .chunking import chunk_text
from .models import Chunk, Document, DocumentStatus, IngestionJob, JobState, KnowledgeBaseStatus
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
        job.state = JobState.SUCCEEDED
        document.status = DocumentStatus.READY
        document.knowledge_base.status = KnowledgeBaseStatus.READY
        session.commit()
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

