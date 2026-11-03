from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import SecretStr
import pytest

from app import payments, site_auth
from app.config import Settings
from app.models import SiteAuditEvent, SitePaymentConfig, User
from test_auth import PASSWORD, register
from test_payments import create, notification

ROOT = "/site/admin/payments"


@pytest.fixture
def gateway(monkeypatch):
    settings = Settings(_env_file=None).model_copy(update={
        "zhifux_enabled": True, "zhifux_api_base_url": "https://gateway.example/api",
        "zhifux_merchant_num": "888888", "zhifux_secret": SecretStr("test-only-secret"),
        "zhifux_pay_type": "aloop", "api_public_url": "https://api.example",
        "frontend_url": "https://app.example",
        "recharge_packages_json": '[{"id":"test","name":"测试档位","amount_fen":1234,"credits":567}]',
    })
    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    monkeypatch.setattr(payments, "start_checkout", lambda _settings, order:
        payments.Checkout("P" + order.id.hex, "https://checkout.example/pay"))
    return settings, []


def admin(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    return owner, db


def payload(version=0, **changes):
    return {
        "version": version, "enabled": True,
        "api_base_url": "https://gateway.example/api", "merchant_num": "888888",
        "pay_type": "aloop", "secret": None, "clear_secret": False,
        "packages": [{"id": "test", "name": "测试档位", "amount_fen": 1234, "credits": 567}],
        **changes,
    }


def test_payment_settings_admin_permissions_and_environment_draft(accounts, gateway):
    (owner, member, anonymous), _db = accounts
    register(owner)
    register(member, "member@example.com")
    for client, code in ((member, 403), (anonymous, 401)):
        assert client.get(ROOT).status_code == code
        assert client.put(ROOT, json=payload()).status_code == code
    settings, _ = gateway
    assert settings.zhifux_secret.get_secret_value() == "test-only-secret"
    response = owner.get(ROOT)
    assert response.status_code == 403  # normal users cannot inspect merchant settings


def test_config_is_encrypted_secret_is_hidden_and_blank_keeps_it(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    key = Fernet.generate_key().decode()
    settings.site_config_encryption_key = SecretStr(key)
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)

    draft = owner.get(ROOT)
    assert draft.status_code == 200
    assert draft.json()["source"] == "environment"
    assert draft.json()["has_secret"] is True
    assert "test-only-secret" not in draft.text

    saved = owner.put(ROOT, json=payload(secret="replacement-secret"))
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1 and saved.json()["source"] == "database"
    assert "replacement-secret" not in saved.text
    row = db.get(SitePaymentConfig, 1)
    assert row.encrypted_secret != "replacement-secret"
    assert site_auth.reveal(row.encrypted_secret) == "replacement-secret"

    kept = owner.put(ROOT, json=payload(version=1, secret=None, enabled=False))
    assert kept.status_code == 200, kept.text
    assert site_auth.reveal(db.get(SitePaymentConfig, 2).encrypted_secret) == "replacement-secret"
    events = db.query(SiteAuditEvent).filter(SiteAuditEvent.action == "payment.settings_updated").all()
    assert len(events) == 2
    fields_by_version = {row.details["version"]: row.details["fields"] for row in events}
    assert fields_by_version == {1: ["secret"], 2: ["enabled"]}
    assert "replacement-secret" not in str([row.details for row in events])


def test_legacy_order_uses_its_payment_config_version_after_rotation(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)

    first = owner.put(ROOT, json=payload(secret="version-one-secret"))
    assert first.status_code == 200, first.text
    catalog = owner.get("/payments/catalog").json()
    assert catalog["enabled"]
    order_response = create(owner)
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()
    from app.models import RechargeOrder
    row = db.get(RechargeOrder, UUID(order["id"]))
    assert row.payment_config_version == 1 and row.provider_key == "zhifux"

    rotated = owner.put(ROOT, json=payload(version=1, secret="version-two-secret"))
    assert rotated.status_code == 200, rotated.text
    assert owner.get("/payments/catalog").json()["enabled"]
    previous_settings = settings.model_copy(update={"zhifux_secret": SecretStr("version-one-secret")})
    paid = owner.get(payments.CALLBACK_PATH, params=notification(order, previous_settings))
    assert paid.status_code == 200 and paid.text == "success"
    assert owner.get(f"/payments/orders/{order['id']}").json()["status"] == "paid"


def test_invalid_settings_and_version_conflict_do_not_create_revision(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    response = owner.put(ROOT, json=payload(api_base_url="http://gateway.example"))
    assert response.status_code == 422
    assert owner.put(ROOT, json=payload(packages=[])).status_code == 422
    assert owner.put(ROOT, json=payload(packages=[
        {"id": "x", "name": "x", "amount_fen": 10, "credits": 1},
        {"id": "x", "name": "y", "amount_fen": 20, "credits": 2},
    ])).status_code == 422
    saved = owner.put(ROOT, json=payload(secret="new-secret"))
    assert saved.status_code == 200
    conflict = owner.put(ROOT, json=payload(version=0, secret="other-secret"))
    assert conflict.status_code == 409
    assert db.query(SitePaymentConfig).count() == 1


def test_empty_encryption_key_is_reported_as_unavailable(accounts, gateway, monkeypatch):
    owner, _db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr("")
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    response = owner.get(ROOT)
    assert response.status_code == 200
    assert response.json()["encryption_ready"] is False

    settings.site_config_encryption_key = SecretStr("not-a-fernet-key")
    invalid_key = owner.get(ROOT)
    assert invalid_key.status_code == 200
    assert invalid_key.json()["encryption_ready"] is False
