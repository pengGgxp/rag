from uuid import UUID
import base64
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
from test_payments import create, custom_order, notification, sign_ezfp

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
        "custom_recharge_enabled": False,
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
    assert owner.put(ROOT, json=payload(packages=[
        {"id": "x", "name": "x", "amount_fen": 10, "credits": 1},
        {"id": "x", "name": "y", "amount_fen": 20, "credits": 2},
    ])).status_code == 422
    saved = owner.put(ROOT, json=payload(secret="new-secret"))
    assert saved.status_code == 200
    conflict = owner.put(ROOT, json=payload(version=0, secret="other-secret"))
    assert conflict.status_code == 409
    assert db.query(SitePaymentConfig).count() == 1


def test_payment_methods_and_custom_rate_are_versioned_per_provider(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    methods = [{"code": "wallet-x", "label": "钱包 X"}, {"code": "bank-y", "label": "银行卡"}]
    saved = owner.put(ROOT, json=payload(secret="rate-test", payment_methods=methods,
        custom_credits_per_yuan=275))
    assert saved.status_code == 200, saved.text
    assert saved.json()["payment_methods"] == methods
    assert saved.json()["custom_credits_per_yuan"] == 275
    row = db.get(SitePaymentConfig, saved.json()["version"])
    assert row.payment_methods == methods and row.custom_credits_per_yuan == 275
    duplicate = owner.put(ROOT, json=payload(version=saved.json()["version"], secret=None,
        payment_methods=[{"code": "same", "label": "甲"}, {"code": "same", "label": "乙"}]))
    assert duplicate.status_code == 422
    assert db.query(SitePaymentConfig).count() == 1


def test_admin_custom_recharge_toggle_controls_public_catalog_and_order_validation(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)

    disabled = owner.put(ROOT, json=payload(secret="custom-toggle-test"))
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["custom_recharge_enabled"] is False
    catalog = owner.get("/payments/catalog").json()
    assert [item["id"] for item in catalog["packages"]] == ["test"]
    assert "custom" not in catalog
    disabled_order = owner.post("/payments/orders", json={"package_id": "custom_amount",
        "custom_amount_fen": 2345, "idempotency_key": "custom-disabled-1",
        "quote": "0" * 64, "payment_method_id": "zhifux_aloop"})
    assert disabled_order.status_code == 422
    assert "未开放自定义充值" in disabled_order.json()["detail"]

    enabled = owner.put(ROOT, json=payload(version=disabled.json()["version"],
        secret=None, custom_recharge_enabled=True))
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["custom_recharge_enabled"] is True
    catalog = owner.get("/payments/catalog").json()
    assert [item["id"] for item in catalog["packages"]] == ["test"]
    assert catalog["custom"]["id"] == "custom_amount"
    assert custom_order(owner, 2300).status_code == 201
    assert db.query(SitePaymentConfig).count() == 2


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
    settings.zhifux_enabled = False
    quote = owner.get("/payments/catalog").json()["packages"][0]["quote"]
    order = owner.post("/payments/orders", json={"package_id": "ezfp-basic", "payment_method_id": "ezfp_wxpay",
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


def test_ezfp_accepts_bare_base64_der_keys_from_merchant_portal(accounts, gateway, monkeypatch):
    owner, db = admin(accounts)
    settings, _ = gateway
    settings.site_config_encryption_key = SecretStr(Fernet.generate_key().decode())
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    platform = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = platform.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    private_base64 = base64.b64encode(private_der).decode("ascii")
    public_base64 = base64.b64encode(public_der).decode("ascii")

    saved = owner.put(f"{ROOT}/ezfp", json=ezfp_payload(
        private_key=private_base64, public_key=public_base64))
    assert saved.status_code == 200, saved.text
    row = db.get(SitePaymentConfig, saved.json()["version"])
    credentials = json.loads(site_auth.reveal(row.encrypted_secret))
    assert "-----BEGIN PRIVATE KEY-----" in credentials["private_key"]
    assert "-----BEGIN PUBLIC KEY-----" in credentials["public_key"]
    assert private_base64 not in saved.text and public_base64 not in saved.text
    assert payments._load_ezfp_key(credentials["private_key"], private=True).key_size == 2048
    assert payments._load_ezfp_key(credentials["public_key"], private=False).key_size == 2048
