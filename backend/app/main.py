import secrets
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.admin.router import router as admin_router
from app.billing.credits import build_router as build_usage_router
from app.cloud_computer.preview import router as cloud_preview_router
from app.cloud_computer.runtime import router as cloud_computer_router
from app.files.preview_access import router as file_preview_access_router
from app.files.quotas import router as storage_billing_router
from app.files.resources import router as resources_router
from app.files.workspace import router as workspace_files_router
from app.infra.config import get_settings
from app.infra.db import SessionLocal, get_db, init_db
from app.integrations.personal_mcp import router as personal_mcp_router
from app.integrations.question_workbench import router as question_workbench_router
from app.model_catalog.shared_providers import selection_router
from app.operations.audit_routes import router as audit_router
from app.operations.deployment import DeploymentMaintenanceMiddleware, router as deployment_router
from app.operations.health import router as health_router
from app.operations.openapi import keep_schema_names
from app.payments.orders import build_router as build_payment_router
from app.search.settings import router as search_settings_router
from app.site.settings import router as site_settings_router
from app.site.suggestions import router as suggestions_router
from app.skills.catalog import catalog_router as platform_skills_router
from app.skills.personal import router as personal_skills_router
from app.skills.uploads import router as skill_uploads_router

from .agent.context_windows import ContextWindowError
from .conversations.documents import router as conversation_documents_router
from .conversations.attachments import router as chat_attachments_router
from .conversations.feedback import router as feedback_router
from .conversations.jobs import recover_interrupted_conversations
from .conversations.routes import router as conversations_router
from .identity import auth_logging  # noqa: F401
from .identity.auth import BrowserRequestGuard, router as auth_router
from .identity.email_auth import router as email_auth_router
from .identity.wechat_auth import router as wechat_auth_router
from .images.routes import router as image_tasks_router
from .knowledge.ingestion import failures as document_failures
from .knowledge.ingestion.uploads import router as document_uploads_router
from .knowledge.routes import router as knowledge_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        from app.operations.site_audit import prune
        prune(db)
        db.commit()
        recover_interrupted_conversations(db)
    async def audit_retention():
        def cleanup():
            from app.operations.site_audit import prune
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
        from .conversations.documents import recover_pending
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
        from app.cloud_computer.runtime import maintenance
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
keep_schema_names(app)


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


app.include_router(health_router)
app.include_router(knowledge_router)
app.include_router(auth_router)
app.include_router(conversation_documents_router)
app.include_router(question_workbench_router)
app.include_router(email_auth_router)
app.include_router(wechat_auth_router)
app.include_router(document_uploads_router)
app.include_router(skill_uploads_router)
app.include_router(chat_attachments_router)
app.include_router(feedback_router)
app.include_router(audit_router)
app.include_router(build_usage_router(get_db))
app.include_router(site_settings_router)
app.include_router(search_settings_router)
app.include_router(workspace_files_router)
app.include_router(resources_router)
app.include_router(file_preview_access_router)
app.include_router(storage_billing_router)
app.include_router(build_payment_router(get_db))
app.include_router(conversations_router)
app.include_router(platform_skills_router)
app.include_router(personal_skills_router)
app.include_router(personal_mcp_router)
app.include_router(suggestions_router)
app.include_router(image_tasks_router)
app.include_router(cloud_computer_router)
app.include_router(cloud_preview_router)
app.include_router(admin_router)
app.include_router(selection_router)
app.include_router(deployment_router)

