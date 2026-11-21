from uuid import UUID, uuid4

from sqlalchemy import select

from app.models import ModelConnection, SiteModelProvider, User, UserModelPolicy
from test_auth import register


def setup_policy_accounts(accounts):
    (admin, member, _), db = accounts
    admin_identity = register(admin, "policy-admin@example.com", "Policy admin")
    member_identity = register(member, "policy-user@example.com", "Policy user")
    db.get(User, UUID(admin_identity["user"]["id"])).is_site_admin = True
    db.commit()
    return admin, member, db, admin_identity, member_identity


def test_site_admin_can_update_user_model_policy_and_normal_user_cannot(accounts):
    admin, member, db, _admin_identity, target_identity = setup_policy_accounts(accounts)
    target_id = target_identity["user"]["id"]

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
    policy = db.get(UserModelPolicy, UUID(target_id))
    assert policy.billing_multiplier_percent == 125
    assert policy.version == 1

    denied = member.put(
        f"/site/admin/users/{target_id}/model-policy",
        json={
            "available_models": [],
            "default_model": None,
            "billing_multiplier_percent": 150,
            "version": 1,
        },
    )
    assert denied.status_code == 403


def test_model_policy_rejects_stale_version_and_invalid_default(accounts):
    admin, _member, db, _admin_identity, target_identity = setup_policy_accounts(accounts)
    target_id = target_identity["user"]["id"]
    model_id = uuid4()

    stale = admin.put(
        f"/site/admin/users/{target_id}/model-policy",
        json={
            "available_models": [],
            "default_model": None,
            "billing_multiplier_percent": 125,
            "version": 1,
        },
    )
    assert stale.status_code == 409

    invalid_default = admin.put(
        f"/site/admin/users/{target_id}/model-policy",
        json={
            "available_models": [],
            "default_model": str(model_id),
            "billing_multiplier_percent": 125,
            "version": 0,
        },
    )
    assert invalid_default.status_code == 422
    assert db.scalar(select(UserModelPolicy).where(UserModelPolicy.user_id == UUID(target_id))) is None


def test_model_policy_rejects_disabled_models(accounts):
    admin, _member, db, _admin_identity, target_identity = setup_policy_accounts(accounts)
    connection = ModelConnection(
        name="disabled-policy-model",
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
        f"/site/admin/users/{target_identity['user']['id']}/model-policy",
        json={
            "available_models": [str(provider.id)],
            "default_model": str(provider.id),
            "billing_multiplier_percent": 100,
            "version": 0,
        },
    )
    assert response.status_code == 422
