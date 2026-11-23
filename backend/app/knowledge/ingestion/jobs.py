from ... import document_failures
from ...credits import release_embedding_hold
from ...models import EmbeddingStatus, JobState


def stop_ingestion_job(db, document, job, reason):
    if job.state == JobState.RUNNING:
        running_chunks = [chunk for chunk in document.chunks
                          if chunk.embedding_status == EmbeddingStatus.RUNNING]
        if running_chunks:
            if not job.embedding_submitting:
                release_embedding_hold(db, document.knowledge_base.account_id, job.id)
            for chunk in running_chunks:
                chunk.embedding_status, chunk.embedding_error = EmbeddingStatus.FAILED, reason
        if job.phase == "parsing" and document_failures.uncertain_submission(document):
            job.failure = document_failures.failure("parsing", "needs_reconciliation",
                "付费解析请求结果需要核对，暂不能重复提交", request_id=str(job.metering_id or job.id))
    job.state, job.error_detail, job.retry_progress = JobState.CANCELLED, reason, None
    job.submission_pending = False
