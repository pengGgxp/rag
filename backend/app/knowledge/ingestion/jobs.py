import logging
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from ...audit_log import record_audit
from ...auth import Access
from ...credits import release_embedding_hold
from ...models import Document, DocumentStatus, EmbeddingStatus, IngestionJob, JobState, KnowledgeBaseStatus
from . import failures as document_failures


def job_matches_original(document, job):
    return (job.original_revision == document.original_revision
            and (job.original_hash is None or job.original_hash == document.content_hash))


def latest_document_job(db, document, *, prefer_ready=False):
    ready = prefer_ready or document.status == DocumentStatus.READY
    ordering = [case(
        (IngestionJob.state.in_((JobState.RUNNING, JobState.QUEUED)), 0),
        (IngestionJob.state == JobState.SUCCEEDED, 1 if ready else 2),
        else_=2 if ready else 1,
    )]
    if ready:
        ordering.append(case((IngestionJob.phase == "indexing", 0), else_=1))
    ordering.extend((IngestionJob.created_at.desc(), IngestionJob.updated_at.desc(), IngestionJob.id.desc()))
    return db.scalar(select(IngestionJob).where(
        IngestionJob.document_id == document.id,
        IngestionJob.original_revision == document.original_revision,
        (IngestionJob.original_hash == document.content_hash) | IngestionJob.original_hash.is_(None),
    ).order_by(*ordering).limit(1))


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


def require_indexing_stopped(document):
    if any(chunk.embedding_status == EmbeddingStatus.RUNNING for chunk in document.chunks):
        raise HTTPException(status_code=409, detail="上一次文档处理尚未结束，请稍后重试")


def reset_abandoned_indexing(db: Session, document, job: IngestionJob):
    for chunk in document.chunks:
        if chunk.embedding_status == EmbeddingStatus.RUNNING:
            chunk.embedding_status = EmbeddingStatus.FAILED
            chunk.embedding_error = "上一次索引任务未完成，请重试"
    release_embedding_hold(db, document.knowledge_base.account_id, job.id)


def lock_ingestion_job(db, job_id, access):
    document_id = db.scalar(select(IngestionJob.document_id).where(IngestionJob.id == job_id))
    if document_id is None:
        raise HTTPException(status_code=404, detail="Job not found")
    document = db.get(Document, document_id, with_for_update=True)
    if document is None or not document.is_active:
        raise HTTPException(status_code=404, detail="Document not found")
    access.library(db, document.knowledge_base_id, write=True)
    job = db.get(IngestionJob, job_id, with_for_update=True, populate_existing=True)
    if job is None or not job_matches_original(document, job):
        raise HTTPException(status_code=404, detail="Job not found")
    return job, document


def cancel_job(
    job_id: UUID,
    request: Request,
    db: Session,
    access: Access,
):
    job, document = lock_ingestion_job(db, job_id, access)
    if job.state not in (JobState.QUEUED, JobState.RUNNING):
        raise HTTPException(status_code=409, detail="Only queued or running jobs can be cancelled")
    stop_ingestion_job(db, document, job, "任务已取消")
    if document.status in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING):
        document.status = DocumentStatus.FAILED
        document.error_detail = "任务已取消"
        document.knowledge_base.status = KnowledgeBaseStatus.ERROR
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="job.cancelled", resource_type="ingestion_job", resource_id=job.id,
        metadata={"document_id": str(job.document_id)}, request=request,
    )
    db.commit()
    db.refresh(job)
    return job


def retry_job(
    job_id: UUID,
    request: Request,
    db: Session,
    access: Access,
):
    old_job, document = lock_ingestion_job(db, job_id, access)
    if document.status == DocumentStatus.DELETED:
        raise HTTPException(status_code=409, detail="请先恢复文档，再重试处理")
    existing = db.scalar(select(IngestionJob).where(IngestionJob.retry_of == old_job.id))
    if existing is not None:
        return existing
    failure_detail = document_failures.safe_job_failure(old_job, document)
    if failure_detail and failure_detail.get("code") == "needs_reconciliation":
        old_job.failure = failure_detail
        old_job.error_detail = failure_detail["message"]
        db.commit()
        raise document_failures.rejection(409, failure_detail["stage"], "needs_reconciliation",
                                          failure_detail["message"])
    if old_job.state not in (JobState.FAILED, JobState.CANCELLED):
        raise HTTPException(status_code=409, detail="Only failed or cancelled jobs can be retried")
    active = db.scalar(select(IngestionJob).where(
        IngestionJob.document_id == document.id, IngestionJob.original_revision == document.original_revision,
        IngestionJob.state.in_((JobState.QUEUED, JobState.RUNNING)),
    ))
    if active is not None:
        raise HTTPException(status_code=409, detail="This document already has an active job")
    reset_abandoned_indexing(db, document, old_job)
    document.status = DocumentStatus.QUEUED
    document.error_detail = None
    document.knowledge_base.status = KnowledgeBaseStatus.PROCESSING
    phase = "indexing" if document.parsed_text and document.chunks else "parsing"
    new_job = IngestionJob(document_id=document.id, retry_of=old_job.id, original_hash=document.content_hash,
                           original_revision=document.original_revision,
                           phase=phase,
                           metering_id=old_job.metering_id or old_job.id,
                           submission_pending=True)
    db.add(new_job)
    db.flush()
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="job.retried", resource_type="ingestion_job", resource_id=new_job.id,
        metadata={"document_id": str(document.id), "previous_job_id": str(old_job.id)}, request=request,
    )
    db.commit()
    db.refresh(new_job)
    try:
        dispatch_retried_job(db, new_job)
    except Exception:
        logging.getLogger(__name__).warning("Ingestion retry dispatch deferred: %s", new_job.id)
    return new_job


def dispatch_retried_job(db: Session, job: IngestionJob):
    from ...tasks import embed_document, process_document

    document = db.get(Document, job.document_id)
    if job.phase == "indexing" and document.parsed_text and document.chunks:
        embed_document.delay(str(document.id), str(job.id))
    else:
        process_document.delay(str(job.id))
