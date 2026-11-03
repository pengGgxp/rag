from uuid import UUID
import json

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from pydantic import SecretStr
import pytest

from app import payments, site_auth
from app.config import Settings
from app.models import SiteAuditEvent, SitePaymentConfig, User
from test_auth import PASSWORD, register
from test_payments import create, notification, sign_ezfp

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
        "provider_key": "zhifux",
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
        assert client.get(f"{ROOT}/ezfp").status_code == code
        assert client.put(f"{ROOT}/ezfp", json=ezfp_payload(enabled=False)).status_code == code
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


def ezfp_payload(version=0, **changes):
    return {"version": version, "provider_key": "ezfp", "enabled": True,
        "api_base_url": payments.PAYMENT_ADAPTERS["ezfp"].endpoint, "merchant_num": "100100",
        "pay_type": "wxpay", "secret": None, "private_key": None, "public_key": None,
        "clear_secret": False,
        "packages": [{"id": "ezfp-basic", "name": "易支付测试档", "amount_fen": 1000, "credits": 1000}],
        **changes}


def test_ezfp_credentials_are_validated_encrypted_and_versioned(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    platform_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = platform_private.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    saved = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(private_key=private_pem, public_key=public_pem))
    assert saved.status_code == 200, saved.text
    assert saved.json()["provider_key"] == "ezfp" and saved.json()["has_secret"]
    assert private_pem not in saved.text and public_pem not in saved.text
    row = db.get(SitePaymentConfig, saved.json()["version"])
    assert row.provider_key == "ezfp"
    assert json.loads(site_auth.reveal(row.encrypted_secret))["private_key"].rstrip() == private_pem.rstrip()

    bad_pair = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(version=saved.json()["version"],
        private_key=private_pem, public_key="-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----"))
    assert bad_pair.status_code == 422
    kept = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(version=saved.json()["version"], enabled=False))
    assert kept.status_code == 200
    assert json.loads(site_auth.reveal(db.get(SitePaymentConfig, kept.json()["version"]).encrypted_secret))["private_key"].rstrip() == private_pem.rstrip()
    assert owner.get(f"{ROOT}/ezfp").json()["website_url"] == "https://www.ezfp.cn/"

    enabled = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(version=kept.json()["version"]))
    assert enabled.status_code == 200
    monkeypatch.setattr(payments.EzfpAdapter, "create_checkout", lambda self, config, order, client_ip="":
        payments.Checkout("ROTATE-1", "https://checkout.example/pay"))
    quote = next(channel for channel in owner.get("/payments/catalog").json()["providers"]
        if channel["provider_key"] == "ezfp")["packages"][0]["quote"]
    order = owner.post("/payments/orders", json={"package_id": "ezfp-basic", "provider_key": "ezfp",
        "idempotency_key": "ezfp-rotation-test", "quote": quote}).json()
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    replacement_pem = replacement.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    replacement_public = replacement.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    rotated = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(version=enabled.json()["version"],
        enabled=False, private_key=replacement_pem, public_key=replacement_public))
    assert rotated.status_code == 200
    # A disabled current channel and rotated platform key cannot invalidate an already-created order.
    callback = sign_ezfp({"pid": "100100", "trade_no": "ROTATE-1", "out_trade_no": order["order_no"],
        "type": "wxpay", "trade_status": "TRADE_SUCCESS", "money": "10.00", "timestamp": "1790000000"}, platform_private)
    paid = owner.get(payments.EZFP_CALLBACK_PATH, params=callback)
    assert paid.status_code == 200 and paid.text == "success"
    assert owner.get(f"/payments/orders/{order['id']}").json()["status"] == "paid"
