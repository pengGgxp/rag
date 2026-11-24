import secrets
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import text

from .knowledge.routes import router as knowledge_router
from .config import get_settings
from .db import SessionLocal, engine, get_db, init_db
from .identity.auth import BrowserRequestGuard, router as auth_router
from .identity.email_auth import router as email_auth_router
from .identity.wechat_auth import router as wechat_auth_router
from .identity.site_auth import router as site_auth_router
from .identity.site_admin import router as site_admin_router
from .identity import auth_logging  # noqa: F401
from .knowledge.ingestion.embedding import OpenAICompatibleEmbeddingProvider
from .storage_settings import router as storage_settings_router
from .knowledge.ingestion.uploads import router as document_uploads_router
from .knowledge.ingestion import failures as document_failures
from .skill_uploads import router as skill_uploads_router
from .conversations.jobs import recover_interrupted_conversations
from .conversations.routes import router as conversations_router
from .conversations.feedback import router as feedback_router
from .audit import router as audit_router
from .credits import build_router as build_usage_router
from .extensions import router as extensions_router
from .payments import build_router as build_payment_router
from .payment_settings import router as payment_settings_router
from .model_pricing import router as model_pricing_router
from .pricing_calculator import router as pricing_calculator_router
from .site_settings import router as site_settings_router
from .search_settings import router as search_settings_router
from .workspace_files import router as workspace_files_router
from .storage_billing import router as storage_billing_router
from .deployment import DeploymentMaintenanceMiddleware, router as deployment_router

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        from .site_audit import prune
        prune(db)
        db.commit()
        recover_interrupted_conversations(db)
    async def audit_retention():
        def cleanup():
            from .site_audit import prune
            with SessionLocal() as db:
                prune(db)
                db.commit()
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(cleanup)
                from .tasks import maintain_uploads
                await asyncio.to_thread(maintain_uploads.delay)
            except Exception:
                logging.getLogger(__name__).warning("Retention maintenance unavailable; retrying next hour")
    async def recover_document_saves():
        from .conversation_documents import recover_pending
        from .tasks import recover_document_ingestion
        while True:
            try:
                await asyncio.to_thread(recover_pending, SessionLocal)
            except Exception:
                logging.getLogger(__name__).warning("Document submission recovery deferred")
            try:
                await asyncio.to_thread(recover_document_ingestion.delay)
            except Exception:
                logging.getLogger(__name__).warning("Ingestion recovery deferred")
            await asyncio.sleep(30)
    try:
        from .tasks import maintain_uploads
        maintain_uploads.delay()
    except Exception:
        logging.getLogger(__name__).warning("Upload recovery deferred until the next maintenance run")
    retention = asyncio.create_task(audit_retention())
    document_recovery = asyncio.create_task(recover_document_saves())
    async def cloud_maintenance():
        from .cloud_computer import maintenance
        while True:
            try:
                await asyncio.to_thread(maintenance, wait=False)
            except Exception:
                logging.getLogger(__name__).warning("Cloud computer maintenance deferred")
            await asyncio.sleep(15)
    computer_recovery = asyncio.create_task(cloud_maintenance())
    async def conversation_maintenance():
        from .conversations.jobs import recover
        while True:
            try:
                await asyncio.to_thread(recover)
            except Exception:
                logging.getLogger(__name__).exception("Conversation recovery deferred")
            await asyncio.sleep(30)
    conversation_recovery = asyncio.create_task(conversation_maintenance())
    try:
        yield
    finally:
        retention.cancel()
        document_recovery.cancel()
        computer_recovery.cancel()
        conversation_recovery.cancel()
        with suppress(asyncio.CancelledError):
            await retention
        with suppress(asyncio.CancelledError):
            await document_recovery
        with suppress(asyncio.CancelledError):
            await computer_recovery
        with suppress(asyncio.CancelledError):
            await conversation_recovery


app = FastAPI(title="RAG Platform API", version="0.1.0", lifespan=lifespan)

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.middleware("http")
async def document_request(request, call_next):
    stage = document_failures.route_stage(request.url.path, request.method)
    if not stage:
        return await call_next(request)
    request.state.document_stage = stage
    request.state.document_request_id = str(uuid4())
    exception_type = None
    sqlstate = None
    try:
        response = await call_next(request)
    except Exception as error:
        exception_type = type(error).__name__
        sqlstate = document_failures.database_sqlstate(error)
        response = document_failures.error_response(request, error)
    response.headers["X-Request-ID"] = request.state.document_request_id
    route = request.scope.get("route")
    params = request.path_params
    route_path = getattr(route, "path", "-")
    common = (
        request.state.document_request_id, stage, route_path, response.status_code,
        response.headers.get("content-type"), params.get("identifier"), params.get("job_id"),
        params.get("library_id") or params.get("knowledge_base_id"),
    )
    if response.status_code >= 400:
        document_failures.log.warning(
            "document_request_failed request_id=%s stage=%s route=%s status=%s content_type=%s upload_id=%s job_id=%s library_id=%s exception_type=%s sqlstate=%s",
            *common, exception_type, sqlstate,
        )
    elif request.method == "POST":
        document_failures.log.info(
            "document_request_succeeded request_id=%s stage=%s route=%s status=%s content_type=%s upload_id=%s job_id=%s library_id=%s request_key=%s",
            *common, request.headers.get("idempotency-key"),
        )
    return response


@app.exception_handler(StarletteHTTPException)
async def document_http_error(request, error):
    if getattr(request.state, "document_stage", None):
        return document_failures.error_response(request, error)
    return await http_exception_handler(request, error)


@app.exception_handler(RequestValidationError)
async def document_validation_error(request, error):
    if getattr(request.state, "document_stage", None):
        return document_failures.error_response(request, HTTPException(422))
    return await request_validation_exception_handler(request, error)


from .context_windows import ContextWindowError


@app.exception_handler(ContextWindowError)
async def context_window_error(_request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409, content={"detail": str(exc), "error_code": exc.code})
settings = get_settings()
app.add_middleware(BrowserRequestGuard)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.auth_secret_key.get_secret_value() if settings.auth_secret_key else secrets.token_urlsafe(48),
    session_cookie="rag_oidc_state", max_age=600,
    https_only=settings.auth_cookie_secure, same_site="lax",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "X-Request-ID", "ETag", "Content-Range"],
)
app.add_middleware(DeploymentMaintenanceMiddleware)


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


@app.get("/health")
def health():
    return _service_health()


@app.get("/ready")
def ready():
    return _service_health(require_worker=True)


@app.get("/version")
def version():
    settings = get_settings()
    return {
        "version": settings.app_version,
        "commit": settings.git_commit,
        "config_contract": settings.config_contract,
        "database_rollback": settings.deployment_rollback,
    }


app.include_router(knowledge_router)
app.include_router(auth_router)
from .conversation_documents import router as conversation_documents_router
app.include_router(conversation_documents_router)
from .question_workbench import router as question_workbench_router
app.include_router(question_workbench_router)
app.include_router(email_auth_router)
app.include_router(wechat_auth_router)
app.include_router(site_auth_router)
app.include_router(site_admin_router)
app.include_router(storage_settings_router)
app.include_router(document_uploads_router)
app.include_router(skill_uploads_router)
from .conversations.attachments import router as chat_attachments_router
app.include_router(chat_attachments_router)
app.include_router(feedback_router)
app.include_router(audit_router)
app.include_router(build_usage_router(get_db))
app.include_router(model_pricing_router)
app.include_router(pricing_calculator_router)
app.include_router(site_settings_router)
app.include_router(search_settings_router)
app.include_router(workspace_files_router)
from .resources import router as resources_router
app.include_router(resources_router)
from .file_preview_access import router as file_preview_access_router
app.include_router(file_preview_access_router)
app.include_router(storage_billing_router)
app.include_router(build_payment_router(get_db))
app.include_router(payment_settings_router)
app.include_router(conversations_router)
app.include_router(extensions_router)
from .site_skills import router as site_skills_router
app.include_router(site_skills_router)
from .suggestions import router as suggestions_router
app.include_router(suggestions_router)
from .shared_providers import router as shared_providers_router, selection_router
app.include_router(shared_providers_router)
from .model_connections import router as model_connections_router
app.include_router(model_connections_router)
from .images.routes import router as image_tasks_router, admin_router as image_models_router
app.include_router(image_tasks_router)
app.include_router(image_models_router)
from .cloud_computer import router as cloud_computer_router
app.include_router(cloud_computer_router)
from .cloud_preview import router as cloud_preview_router
app.include_router(cloud_preview_router)
from .thinking_admin import router as thinking_admin_router
app.include_router(thinking_admin_router)
app.include_router(selection_router)
app.include_router(deployment_router)

