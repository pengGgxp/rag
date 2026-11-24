import subprocess
import sys

import pytest
from starlette.routing import Match

from app.admin.router import router
from app.identity.site_auth import site_admin
from app.main import app


def _uses_site_admin(node):
    return node.call is site_admin or any(_uses_site_admin(child) for child in node.dependencies)


def test_all_site_admin_routes_are_registered_through_admin_router():
    def entries(routes):
        return {(route.path, frozenset(route.methods), route.endpoint) for route in routes}

    protected_routes = [route for route in app.routes
                        if hasattr(route, "dependant") and _uses_site_admin(route.dependant)]
    assert entries(router.routes) == entries(protected_routes)
    assert {route.path for route in protected_routes} >= {"/site/settings/auth", "/site/admin/audit"}


@pytest.mark.parametrize(("path", "method", "expected"), [
    ("/site/admin/payments/recharge", "GET", "/site/admin/payments/recharge"),
    ("/site/admin/payments/recharge", "PUT", "/site/admin/payments/recharge"),
    ("/site/admin/model-connections/platforms", "GET", "/site/admin/model-connections/platforms"),
    ("/site/admin/model-connections/model-presets", "GET", "/site/admin/model-connections/model-presets"),
    ("/site/admin/model-connections/image-presets", "GET", "/site/admin/model-connections/image-presets"),
    ("/site/admin/skills/uploads", "POST", "/site/admin/skills/uploads"),
])
def test_static_admin_paths_match_before_parameterized_routes(path, method, expected):
    scope = {"type": "http", "method": method, "path": path}
    route = next(route for route in app.routes if route.matches(scope)[0] == Match.FULL)
    assert route.path == expected


@pytest.mark.parametrize(("path", "method", "name"), [
    ("/site/admin/cloud-computer", "put", "app__cloud_computer__SettingsInput"),
    ("/site/admin/settings", "put", "app__site_settings__SettingsInput"),
    ("/site/admin/storage/test", "post", "app__storage_settings__TestInput"),
    ("/site/admin/llm-providers/{identifier}/thinking/test", "post", "app__thinking_admin__TestInput"),
    ("/site/admin/skills/uploads", "post", "app__skill_uploads__UploadInput"),
])
def test_openapi_preserves_published_component_names(path, method, name):
    spec = app.openapi()
    body = spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    assert body["$ref"] == f"#/components/schemas/{name}"
    assert name in spec["components"]["schemas"]


def test_worker_and_preview_do_not_import_admin_assembly():
    subprocess.run([sys.executable, "-c", """
import sys
from app.tasks import celery_app
from app.preview_gateway import app
assert "app.main" not in sys.modules
assert "app.admin.router" not in sys.modules
"""], check=True, timeout=30)
