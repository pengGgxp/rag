"""集中登记站点后台路由，业务处理由各模块负责。"""

from fastapi import APIRouter

from app.billing.calculator import router as calculator_router
from app.billing.pricing import router as pricing_router
from app.cloud_computer.runtime import admin_router as cloud_router
from app.files.quotas import admin_router as quotas_router
from app.files.settings import router as storage_router
from app.identity.site_admin import router as accounts_router
from app.identity.site_auth import router as authentication_router
from app.images.routes import admin_router as image_models_router
from app.model_catalog.connections import router as connections_router
from app.model_catalog.shared_providers import router as providers_router
from app.model_catalog.thinking_admin import router as thinking_router
from app.payments.settings import router as payments_router
from app.search.settings import admin_router as search_router
from app.site.settings import admin_router as site_router
from app.site.suggestions import admin_router as suggestions_router
from app.skills.catalog import router as skills_router
from app.skills.uploads import admin_router as skill_uploads_router

from .audit_routes import router as audit_router

router = APIRouter()
for domain_router in (
    authentication_router, accounts_router, audit_router, storage_router,
    skill_uploads_router, pricing_router, calculator_router, site_router,
    search_router, quotas_router, payments_router, skills_router,
    suggestions_router, providers_router, connections_router, image_models_router,
    cloud_router, thinking_router,
):
    router.include_router(domain_router)
