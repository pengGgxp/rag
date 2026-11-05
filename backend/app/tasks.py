from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import select
from uuid import UUID
from datetime import datetime, timezone
import io

from .config import get_settings
from .auth import aware
from .db import SessionLocal
from .docmind import (
    AliyunDocMind, DocMindError, PARSER_NAME, ParsingCancelled,
    MAX_SUBMISSION_BYTES, pdf_part, submission_ranges,
)
from .parsed_document import ParsedDocument, assemble_pages, parsed_chunks
from .embedding import EmbeddingConfigurationError, OpenAICompatibleEmbeddingProvider
from .models import (
    Chunk,
    Document,
    DocumentStatus,
    EmbeddingStatus,
    IngestionJob,
    JobState,
    KnowledgeBaseStatus,
    SiteStorageSettings,
    WorkspaceFile,
)
from .parsers import parse_document, parse_local_pdf, pdf_pages
from .storage import download_fileobj
from .credits import (
    authorization_hold, authorization_hold_amount, release_hold, settle_usage, reconcile_hold,
)
from .workspace_billing import document_snapshot

celery_app = Celery("rag", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", result_serializer="json")


@celery_app.task
def process_image_task(identifier):
    from .image_tasks import run
    run(identifier)


@celery_app.task
def recover_image_tasks():
    from .image_tasks import recover
    recover()


@worker_ready.connect
def image_tasks_on_worker_ready(**_):
    recover_image_tasks.delay()


def ocr_storage(session):
    from .cos_storage import CosStorage
    settings = session.get(SiteStorageSettings, 1)
    return CosStorage(settings) if settings and settings.active else None


def cleanup_ocr_object(storage, key):
    if storage and key:
        try:
            storage.delete(key)
        except Exception:
            # Retain the key in the parsing checkpoint for later cleanup.
            return False
    return True


def parse_for_ingestion(session, job, document, content: bytes) -> ParsedDocument:
    if not (document.filename.lower().endswith(".pdf") or document.content_type == "application/pdf"):
        text, name = parse_document(document.filename, document.content_type, content)
        return ParsedDocument(text, name)
    settings = get_settings()
    metadata = document.parsing_metadata or {}
    if metadata.get("status") == "complete" and document.parsed_text:
        return ParsedDocument(document.parsed_text, document.parser_name, metadata["pages"])
    if settings.pdf_parser == "local" and not metadata.get("task_id") and not metadata.get("parts"):
        return parse_local_pdf(content)

    pages = pdf_pages(content)
    page_count = len(pages)
    if not metadata.get("task_id") and metadata.get("status") != "submitting":
        ranges = ([(part["start"], part["end"]) for part in metadata["parts"]]
                  if metadata.get("parts") else submission_ranges(content, pages))
        if len(ranges) > 1 or len(content) > MAX_SUBMISSION_BYTES or metadata.get("parts"):
            return parse_batched_pdf(session, job, document, pages, ranges, settings)
    provider = AliyunDocMind(settings)
    workspace_id = document.knowledge_base.workspace_id
    pricing_context = document_snapshot(session, workspace_id, job.id, "ocr_pages", "ocr")
    ocr_hold = False
    hold_amount = authorization_hold_amount()

    def check_active():
        session.refresh(job)
        session.refresh(document)
        if job.state != JobState.RUNNING or document.status == DocumentStatus.DELETED:
            raise ParsingCancelled()

    check_active()
    if not metadata.get("task_id") and metadata.get("status") == "submitting":
        raise DocMindError("上次云端提交结果未知，已阻止重复付费提交，请由管理员核对云端任务")
    source_url = None
    if not metadata.get("task_id"):
        storage = ocr_storage(session)
        source_url = storage.read_url(document.object_key) if storage else None
    authorization_hold(session, workspace_id, None, job.id, hold_amount)
    session.commit()
    ocr_hold = True
    if not metadata.get("task_id"):
        metadata = {
            "provider": PARSER_NAME, "status": "submitting", "page_count": page_count,
            "config": {"llm_enhancement": False, "formula_enhancement": False,
                       "output_format": ["markdown", "visualLayoutInfo"]},
        }
        document.parsing_metadata = metadata
        session.commit()
        try:
            task_id = provider.submit_url(source_url, page_count) if source_url else provider.submit(content, page_count)
        except DocMindError as exc:
            if not exc.uncertain:
                # An explicit rejection is safe to retry after configuration is fixed.
                document.parsing_metadata = None
                release_hold(session, workspace_id, None, job.id, hold_amount)
                session.commit()
            raise
        except Exception:
            release_hold(session, workspace_id, None, job.id, hold_amount)
            session.commit()
            raise
        document.parsing_metadata = {**metadata, "status": "processing", "task_id": task_id}
        session.commit()
        metadata = document.parsing_metadata
    try:
        parsed = provider.collect(metadata["task_id"], page_count, check_active)
    except Exception:
        if ocr_hold:
            release_hold(session, workspace_id, None, job.id, authorization_hold_amount())
            session.commit()
        raise
    check_active()
    event = settle_usage(
        session, workspace_id, None, job.id, "ocr_pages", page_count,
        event_key="ocr", metadata={"document_id": str(document.id), "provider": PARSER_NAME},
        pricing_context=pricing_context,
    )
    reconcile_hold(
        session, workspace_id, None, job.id, event.credits, hold_amount,
    )
    session.commit()
    document.parsed_text = parsed.text
    document.parser_name = parsed.parser_name
    document.parsing_metadata = {**metadata, "status": "complete", "pages": parsed.pages}
    session.commit()
    return parsed


def parse_batched_pdf(session, job, document, pages, ranges, settings) -> ParsedDocument:
    """Persist each paid submission and its result before moving to the next batch."""
    import copy

    provider = AliyunDocMind(settings)
    workspace_id = document.knowledge_base.workspace_id
    pricing_context = document_snapshot(session, workspace_id, job.id, "ocr_pages", "ocr")
    metadata = copy.deepcopy(document.parsing_metadata or {
        "provider": PARSER_NAME, "status": "processing", "page_count": len(pages),
        "parts": [{"start": start, "end": end, "status": "pending"} for start, end in ranges],
        "config": {"llm_enhancement": False, "formula_enhancement": False,
                   "output_format": ["markdown", "visualLayoutInfo"]},
    })

    def check_active():
        session.refresh(job)
        session.refresh(document)
        if job.state != JobState.RUNNING or document.status == DocumentStatus.DELETED:
            raise ParsingCancelled()

    def save():
        document.parsing_metadata = copy.deepcopy(metadata)
        session.commit()

    check_active()
    if any(part["status"] == "submitting" and not part.get("task_id") for part in metadata["parts"]):
        raise DocMindError("上次云端提交结果未知，已阻止重复付费提交，请由管理员核对云端任务")
    hold_amount = authorization_hold_amount()
    authorization_hold(session, workspace_id, None, job.id, hold_amount)
    session.commit()
    combined = []
    try:
        for part in metadata["parts"]:
            check_active()
            if part["status"] != "complete":
                if not part.get("task_id"):
                    content = pdf_part(pages, part["start"], part["end"])
                    storage = ocr_storage(session)
                    source_url = None
                    if storage:
                        part["object_key"] = f"_ocr/{document.id}/{job.id}/{part['start']}-{part['end']}.pdf"
                        # Persist before uploading so an interrupted submission is cleanable.
                        save()
                        storage.put(part["object_key"], io.BytesIO(content), "application/pdf")
                        source_url = storage.read_url(part["object_key"])
                    part["status"] = "submitting"
                    save()
                    try:
                        count = part["end"] - part["start"]
                        part["task_id"] = provider.submit_url(source_url, count) if source_url else provider.submit(content, count)
                    except DocMindError as exc:
                        if not exc.uncertain:
                            part["status"] = "pending"
                            save()
                        raise
                    finally:
                        del content
                    part["status"] = "processing"
                    save()
                parsed = provider.collect(part["task_id"], part["end"] - part["start"], check_active)
                check_active()
                part.update(status="complete", text=parsed.text, pages=parsed.pages)
                save()
                if cleanup_ocr_object(ocr_storage(session), part.get("object_key")):
                    part.pop("object_key", None)
                    save()
            for page in part["pages"]:
                combined.append((part["start"] + page["page_number"], [
                    {**block, "text": part["text"][block["char_start"]:block["char_end"]]}
                    for block in page["blocks"]
                ]))
        check_active()
        parsed = assemble_pages(combined, PARSER_NAME)
        if not parsed.text.strip():
            raise DocMindError("阿里云解析未返回可检索文字，请检查扫描清晰度或文档类型")
        event = settle_usage(
            session, workspace_id, None, job.id, "ocr_pages", len(pages), event_key="ocr",
            metadata={"document_id": str(document.id), "provider": PARSER_NAME},
            pricing_context=pricing_context,
        )
        reconcile_hold(session, workspace_id, None, job.id, event.credits, hold_amount)
        document.parsed_text = parsed.text
        document.parser_name = parsed.parser_name
        # Final text/locations replace the temporary per-batch caches.
        for part in metadata["parts"]:
            part.pop("text", None)
            part.pop("pages", None)
        metadata.update(status="complete", pages=parsed.pages)
        save()
        return parsed
    except Exception:
        release_hold(session, workspace_id, None, job.id, hold_amount)
        session.commit()
        raise


@celery_app.task(bind=True, autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def process_document(self, job_id: str, *, prepared_content: bytes | None = None) -> None:
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

        if prepared_content is None:
            with download_fileobj(document.object_key) as source:
                content = source.read()
        else:
            content = prepared_content
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


@celery_app.task
def verify_upload(identifier: str) -> None:
    from .document_uploads import finalize_upload
    finalize_upload(identifier)


@celery_app.task
def maintain_uploads() -> None:
    from .resource_cache import prune_cache
    prune_cache()
    from .document_uploads import cleanup_uploads, cleanup_orphans, recover_uploads
    cleanup_uploads()
    recover_uploads()
    cleanup_orphans()
    cleanup_workspace_files()
    with SessionLocal() as db:
        from .storage_billing import maintenance
        maintenance(db)
    with SessionLocal() as db:
        from .resources import verify_pending_objects
        from .storage import head_object
        verify_pending_objects(db, head_object)
    with SessionLocal() as db:
        storage = ocr_storage(db)
        if storage:
            from .chat_attachments import cleanup
            cleanup(db, storage)
            for document in db.scalars(select(Document).where(Document.parsing_metadata.is_not(None))):
                metadata = document.parsing_metadata or {}
                changed = False
                for part in metadata.get("parts", []):
                    if part.get("object_key") and (part["status"] == "complete" or document.status == DocumentStatus.DELETED):
                        if cleanup_ocr_object(storage, part["object_key"]):
                            part.pop("object_key")
                            changed = True
                if changed:
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(document, "parsing_metadata")
                    db.commit()


def cleanup_workspace_files() -> None:
    """Collect explicitly deleted resource objects; valid resources never expire."""
    from .storage import delete_object
    with SessionLocal() as db:
        expired = db.scalars(select(WorkspaceFile).where(WorkspaceFile.deleted_at.is_not(None))).all()
        for row in expired:
            from .storage_billing import lock_workspace
            lock_workspace(db, row.workspace_id)
            db.refresh(row)
            if row.deleted_at is None:
                continue
            for version in row.versions:
                if version.deleted_at is not None:
                    continue
                if db.scalar(select(Document.id).where(Document.object_key == version.object_key)):
                    continue
                try:
                    delete_object(version.object_key)
                except Exception:
                    continue
                version.deleted_at = datetime.now(timezone.utc)
                from .models import ChatAttachment
                for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.object_key == version.object_key)):
                    attachment.deleted_at = row.deleted_at
                    attachment.object_deleted_at = version.deleted_at
            if all(version.deleted_at is not None for version in row.versions):
                row.deleted_at = datetime.now(timezone.utc)
        db.commit()


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
        workspace_id = document.knowledge_base.workspace_id
        metering_id = UUID(str(job.id if job is not None else document.id))
        pricing_context = document_snapshot(session, workspace_id, metering_id, "embedding_tokens", "embedding")
        hold_amount = authorization_hold_amount()
        authorization_hold(session, workspace_id, None, metering_id, hold_amount)
        session.commit()
        try:
            vectors = provider.embed_texts([chunk.text for chunk in pending])
        except Exception:
            release_hold(session, workspace_id, None, metering_id, hold_amount)
            session.commit()
            raise
        if len(vectors) != len(pending):
            raise ValueError("Embedding provider returned an unexpected number of vectors")
        event = settle_usage(
            session, workspace_id, None, metering_id, "embedding_tokens",
            sum(max(chunk.token_count, 0) for chunk in pending),
            event_key="embedding", metadata={"document_id": str(document.id)},
            pricing_context=pricing_context,
        )
        reconcile_hold(
            session, workspace_id, None, metering_id, event.credits, hold_amount,
        )
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

