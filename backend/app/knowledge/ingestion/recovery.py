"""Keep a worker claim across checkpoint commits without a stale-job timeout."""
import hashlib
from contextlib import contextmanager

from sqlalchemy import select, text

from ...auth import now
from .failures import failure, uncertain_submission
from .jobs import job_matches_original
from ...models import Document, DocumentStatus, EmbeddingStatus, IngestionJob, JobState, KnowledgeBaseStatus


@contextmanager
def worker_claim(db, job_id):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield True
        return
    key = int.from_bytes(hashlib.sha256(f"ingestion:{job_id}".encode()).digest()[:8], "big", signed=True)
    with bind.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        try:
            yield acquired
        finally:
            if acquired and not connection.invalidated:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def recover_interrupted_ingestion(db):
    candidates = db.execute(select(IngestionJob.id, IngestionJob.document_id).where(
        IngestionJob.state == JobState.RUNNING, IngestionJob.updated_at < now(),
    )).all()
    db.rollback()
    for job_id, document_id in candidates:
        with worker_claim(db, job_id) as acquired:
            if not acquired:
                continue
            document = db.get(Document, document_id, with_for_update=True)
            job = db.get(IngestionJob, job_id, with_for_update=True)
            if not document or not job or job.state != JobState.RUNNING:
                db.rollback()
                continue
            if (not document.is_active or document.status == DocumentStatus.DELETED
                    or not job_matches_original(document, job)):
                job.state = JobState.CANCELLED
            elif job.embedding_submitting or uncertain_submission(document):
                job.state = JobState.FAILED
                job.failure = failure(
                    job.phase or "parsing", "needs_reconciliation",
                    "付费处理请求结果需要核对，暂不能重复提交",
                    request_id=str(job.metering_id or job.id),
                )
                document.status = DocumentStatus.FAILED
                document.error_detail = job.error_detail = job.failure["message"]
                for chunk in document.chunks:
                    if chunk.embedding_status == EmbeddingStatus.RUNNING:
                        chunk.embedding_status = EmbeddingStatus.FAILED
                        chunk.embedding_error = job.failure["message"]
                if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
                    document.knowledge_base.status = KnowledgeBaseStatus.ERROR
            else:
                job.state = JobState.QUEUED
                job.submission_pending = True
            db.commit()
