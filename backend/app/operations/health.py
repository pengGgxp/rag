import redis
from fastapi import APIRouter
from sqlalchemy import text

from app.infra.config import get_settings
from app.infra.db import engine
from app.knowledge.ingestion.embedding import OpenAICompatibleEmbeddingProvider

router = APIRouter()


def _service_health(require_worker: bool = False):
    settings = get_settings()
    db_ok = False
    redis_ok = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    try:
        redis_ok = bool(redis.Redis.from_url(settings.redis_url).ping())
    except Exception:
        pass
    worker_ok = False
    try:
        worker_ok = bool(redis.Redis.from_url(settings.redis_url).exists("deploy:worker-heartbeat"))
    except Exception:
        pass
    overall = db_ok and redis_ok and (worker_ok or not require_worker)
    return {
        "status": "ok" if overall else "degraded",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "worker": "ok" if worker_ok else "error",
        "embedding": OpenAICompatibleEmbeddingProvider().status.__dict__,
        "llm": {"configuration_source": "database"},
    }


@router.get("/health")
def health():
    return _service_health()


@router.get("/ready")
def ready():
    return _service_health(require_worker=True)


@router.get("/version")
def version():
    settings = get_settings()
    return {
        "version": settings.app_version,
        "commit": settings.git_commit,
        "config_contract": settings.config_contract,
        "database_rollback": settings.deployment_rollback,
    }
