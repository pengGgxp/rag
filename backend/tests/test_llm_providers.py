from uuid import uuid4

from test_auth import accounts, register


def test_user_provider_configuration_routes_are_removed(accounts):
    (owner, _, _), _db = accounts
    register(owner)
    for method, path in (
        ("GET", "/settings/llm-providers"),
        ("GET", "/settings/llm-providers/current"),
        ("POST", "/settings/llm-providers"),
        ("POST", f"/settings/llm-providers/{uuid4()}/activate"),
        ("DELETE", f"/settings/llm-providers/{uuid4()}"),
    ):
        assert owner.request(method, path, json={}).status_code == 404
