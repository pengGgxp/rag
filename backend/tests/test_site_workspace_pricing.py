from uuid import UUID, uuid4

from app.models import SiteModelProvider
from test_auth import register
from test_site_admin import setup
from test_user_model_policy import setup_policy_accounts


def test_workspace_management_and_legacy_usage_routes_are_removed(accounts):
    admin, owner, anonymous, db, _a, b = setup(accounts)
    workspace = b["user"]["id"]
    for method, suffix in (
        ("GET", ""),
        ("POST", ""),
        ("PUT", f"/{uuid4()}"),
        ("DELETE", f"/{uuid4()}"),
    ):
        path = f"/site/admin/workspaces/{workspace}/llm-providers" + suffix
        assert owner.request(method, path, json={}).status_code == 404
        assert anonymous.request(method, path, json={}).status_code == 404
    assert admin.get(f"/site/admin/workspaces/{workspace}").status_code == 404
    assert owner.get("/usage/policies").status_code == 404
    assert owner.put("/usage/policies/embedding_tokens", json={}).status_code == 404


def test_shared_provider_management_remains_site_admin_only(accounts):
    admin, owner, anonymous, db, _a, _b = setup(accounts)
    provider_id = uuid4()
    for method, suffix in (
        ("GET", ""),
        ("POST", ""),
        ("PUT", f"/{provider_id}"),
        ("DELETE", f"/{provider_id}"),
    ):
        path = "/site/admin/llm-providers" + suffix
        assert owner.request(method, path, json={}).status_code == 403
        assert anonymous.request(method, path, json={}).status_code == 401
    assert admin.get("/site/admin/llm-providers").status_code == 200


def test_user_detail_owns_balance_and_model_policy(accounts):
    admin, member, _anonymous, db, _a, _b = setup(accounts)
    target = register(member, "policy-target@example.com", "Policy target")
    target_id = UUID(target["user"]["id"])
    response = admin.put(
        f"/site/admin/users/{target_id}/model-policy",
        json={
            "available_models": [],
            "default_model": None,
            "billing_multiplier_percent": 125,
            "version": 0,
        },
    )
    assert response.status_code == 200, response.text
    detail = admin.get(f"/site/admin/users/{target_id}")
    assert detail.status_code == 200
    assert "workspaces" not in detail.json()
    assert detail.json()["model_policy"]["billing_multiplier_percent"] == 125
    assert member.put(
        f"/site/admin/users/{target_id}/model-policy",
        json={
            "available_models": [],
            "default_model": None,
            "billing_multiplier_percent": 150,
            "version": 1,
        },
    ).status_code == 403


def test_user_policy_rejects_disabled_site_models(accounts):
    admin, owner, _anonymous, db, _a, _b = setup(accounts)
    target = register(owner, "disabled-policy-target@example.com", "Disabled target")
    from app.models import ModelConnection

    connection = ModelConnection(
        name="disabled-policy-connection",
        base_url="https://provider.example/v1",
        encrypted_api_key="encrypted",
        enabled=True,
    )
    db.add(connection)
    db.flush()
    provider = SiteModelProvider(
        connection_id=connection.id,
        key="disabled-policy-model",
        name="Disabled policy model",
        model="disabled-model",
        enabled=False,
        supports_chat=True,
    )
    db.add(provider)
    db.commit()
    response = admin.put(
        f"/site/admin/users/{target['user']['id']}/model-policy",
        json={
            "available_models": [str(provider.id)],
            "default_model": str(provider.id),
            "billing_multiplier_percent": 100,
            "version": 0,
        },
    )
    assert response.status_code == 422
