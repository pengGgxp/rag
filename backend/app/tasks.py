import threading
from datetime import datetime, timezone

import redis
from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from .config import get_settings
from .db import SessionLocal

celery_app = Celery("rag", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", result_serializer="json")

_heartbeat_stop = threading.Event()
_heartbeat_lock = threading.Lock()
_heartbeat_thread: threading.Thread | None = None


@celery_app.task
def execute_conversation(identifier):
    from .conversations.jobs import run
    run(identifier)


@celery_app.task
def name_conversation(identifier):
    from .conversations.jobs import title_run
    title_run(identifier)


@celery_app.task
def run_cloud_command(identifier):
    from .cloud_computer import run
    run(identifier)


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
    _start_worker_heartbeat()


@worker_shutdown.connect
def worker_heartbeat_on_shutdown(**_):
    _stop_worker_heartbeat()


def _record_heartbeat():
    try:
        redis.Redis.from_url(get_settings().redis_url).set(
            "deploy:worker-heartbeat",
            datetime.now(timezone.utc).isoformat(),
            ex=30,
        )
    except redis.RedisError:
        # Worker processing must continue even when the health marker is unavailable.
        pass


def _heartbeat_loop():
    while not _heartbeat_stop.wait(10):
        _record_heartbeat()


def _start_worker_heartbeat():
    """Refresh the health marker for as long as this worker process is alive."""
    global _heartbeat_thread
    _record_heartbeat()
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_stop.clear()
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name="worker-heartbeat",
            daemon=True,
        )
        _heartbeat_thread.start()


def _stop_worker_heartbeat():
    global _heartbeat_thread
    _heartbeat_stop.set()
    with _heartbeat_lock:
        thread = _heartbeat_thread
        _heartbeat_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


@celery_app.task(bind=True, max_retries=3)
def process_document(self, job_id: str, *, prepared_content: bytes | None = None) -> None:
    from .knowledge.ingestion import pipeline
    pipeline.process_document(self, job_id, prepared_content=prepared_content)


@celery_app.task
def verify_upload(identifier: str) -> None:
    from .knowledge.ingestion.uploads import finalize_upload
    finalize_upload(identifier)


@celery_app.task
def recover_document_ingestion() -> None:
    from .knowledge.ingestion.uploads import recover_uploads
    recover_uploads()


@celery_app.task
def maintain_uploads() -> None:
    from .resource_cache import prune_cache
    prune_cache()
    from .knowledge.ingestion.uploads import cleanup_uploads, cleanup_orphans, recover_uploads
    cleanup_uploads()
    from .skill_uploads import cleanup_expired
    cleanup_expired()
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
    from .knowledge.ingestion.pipeline import cleanup_parsing_objects
    cleanup_parsing_objects(SessionLocal)


def cleanup_workspace_files() -> None:
    from .file_operations import recover_pending
    with SessionLocal() as db:
        recover_pending(db)


@celery_app.task
def embed_document(document_id: str, job_id: str) -> None:
    from .knowledge.ingestion import pipeline
    pipeline.embed_document(document_id, job_id)
