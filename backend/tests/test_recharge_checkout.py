"""Cashier quotes, unified prices, routing and durable recovery."""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.payments import orders as payments
from app.models import RechargeOrder, SiteRechargeConfig, SiteAuditEvent
from test_auth import register, share, switch
from test_payment_settings import admin
from test_payments import gateway, notification

ROOT = "/site/admin/payments/recharge"


def config(**changes):
    return {"version": 0,
        "packages": [{"id": "site", "name": "标准档", "amount_fen": 1200, "credits": 1500}],
        "custom_recharge_enabled": True, "custom_credits_per_yuan": 250,
        "payment_methods": [
            {"id": "alipay", "label": "支付宝", "provider_key": "zhifux", "code": "aloop", "enabled": True},
            {"id": "wechat", "label": "微信支付", "provider_key": "zhifux", "code": "wxpay", "enabled": True}],
        **changes}


def selection(client):
    package = client.get("/payments/catalog").json()["packages"][0]
    return {"package_id": package["id"], "quote": package["quote"]}


def submit(client, chosen=None, **changes):
    return client.post("/payments/orders", json={**(chosen or selection(client)),
        "payment_method_id": "alipay", "idempotency_key": uuid4().hex, **changes})


def test_settings_permissions_validation_conflict_and_audit(accounts, gateway):
    (owner, member, anon), db = accounts
    assert anon.get(ROOT).status_code == 401
    register(member, "member@example.com")
    assert member.get(ROOT).status_code == 403
    owner, db = admin(accounts)
    assert owner.get(ROOT).json()["version"] == 0
    assert owner.put(ROOT, json=config()).status_code == 200
    assert owner.put(ROOT, json=config()).status_code == 409
    assert owner.put(ROOT, json=config(version=1, payment_methods=[
        {"id": "bad", "label": "坏方式", "provider_key": "forged", "code": "x", "enabled": True}])).status_code == 422
    assert owner.put(ROOT, json=config(version=1, custom_credits_per_yuan=True)).status_code == 422
    methods = config()["payment_methods"]
    assert owner.put(ROOT, json=config(version=1, payment_methods=methods + methods)).status_code == 422
    assert db.query(SiteRechargeConfig).count() == 1
    audit = db.scalar(select(SiteAuditEvent).where(SiteAuditEvent.action == "payment.recharge_settings_updated"))
    assert "secret" not in str(audit.details)


def test_preview_never_creates_order_and_hides_provider(accounts, gateway):
    owner, db = admin(accounts)
    assert owner.put(ROOT, json=config()).status_code == 200
    chosen = selection(owner)
    for _ in range(3):
        preview = owner.post("/payments/checkout-preview", json=chosen)
        assert preview.status_code == 200, preview.text
        assert preview.json()["amount_fen"] == 1200 and preview.json()["credits"] == 1500
        assert preview.json()["payment_methods"] == [{"id": "alipay", "label": "支付宝"}, {"id": "wechat", "label": "微信支付"}]
        assert "zhifux" not in preview.text and "aloop" not in preview.text
    assert "providers" not in owner.get("/payments/catalog").json()
    assert db.query(RechargeOrder).count() == 0 and not gateway[1]
    assert owner.post("/payments/checkout-preview", json={**chosen, "credits": 9999}).status_code == 422


def test_new_quote_requires_confirmation_and_custom_uses_global_rate(accounts, gateway):
    owner, _ = admin(accounts)
    owner.put(ROOT, json=config())
    old = selection(owner)
    changed = config(version=1)
    changed["packages"][0]["credits"] = 1800
    assert owner.put(ROOT, json=changed).status_code == 200
    assert owner.post("/payments/checkout-preview", json=old).status_code == 409
    assert submit(owner, old).status_code == 409
    custom = owner.get("/payments/catalog").json()["custom"]
    chosen = {"package_id": custom["id"], "quote": custom["quote"], "custom_amount_fen": 1200}
    assert submit(owner, chosen).json()["credits"] == 3000
    assert submit(owner, {**chosen, "custom_amount_fen": 1250}).status_code == 422
    changed.update(version=2, custom_recharge_enabled=False)
    owner.put(ROOT, json=changed)
    assert submit(owner, chosen).status_code == 422


def test_method_revocation_and_mapping_change_preserve_original_order(accounts, gateway):
    owner, db = admin(accounts)
    owner.put(ROOT, json=config())
    chosen = selection(owner)
    key = uuid4().hex
    first = submit(owner, chosen, idempotency_key=key)
    assert first.status_code == 201, first.text
    order = first.json()
    row = db.get(RechargeOrder, UUID(order["id"]))
    assert row.pay_type == "aloop" and row.recharge_config_version == 1
    updated = config(version=1, payment_methods=[{**config()["payment_methods"][1], "enabled": False}])
    owner.put(ROOT, json=updated)
    assert submit(owner, chosen).status_code == 409
    assert submit(owner, chosen, idempotency_key=key).json()["id"] == order["id"]
    assert submit(owner, chosen, idempotency_key=key, payment_method_id="wechat").status_code == 409
    assert owner.get(payments.CALLBACK_PATH, params=notification(order, gateway[0], amount="12.00")).text == "success"
    assert owner.get(payments.CALLBACK_PATH, params=notification(order, gateway[0], amount="12.00")).text == "success"
    assert owner.get("/usage").json()["balance"] == 2500


def test_attempt_recovery_is_scoped_and_does_not_replay_gateway(accounts, gateway):
    owner, db = admin(accounts)
    (_owner, member, anon), _ = accounts
    owner.put(ROOT, json=config())
    key = uuid4().hex
    assert owner.get(f"/payments/order-attempts/{key}").json() is None
    chosen = selection(owner)
    first = submit(owner, chosen, idempotency_key=key).json()
    assert owner.get(f"/payments/order-attempts/{key}").json()["id"] == first["id"]
    assert submit(owner, chosen, idempotency_key=key).json()["id"] == first["id"]
    assert len(gateway[1]) == 1
    assert anon.get(f"/payments/order-attempts/{key}").status_code == 401
    session = register(member, "other@example.com")
    assert member.get(f"/payments/order-attempts/{key}").json() is None
    assert member.get(f"/payments/orders/{first['id']}").status_code == 404


def test_global_prices_do_not_depend_on_legacy_provider_packages(accounts, gateway):
    owner, _ = admin(accounts)
    owner.put(ROOT, json=config())
    gateway[0].recharge_packages_json = "invalid"
    gateway[0].zhifux_custom_credits_per_yuan = 9999
    assert owner.get("/payments/catalog").json()["custom"]["credits_per_yuan"] == 250
    assert submit(owner).json()["credits"] == 1500
    gateway[0].zhifux_enabled = False
    assert owner.get("/payments/catalog").json() == {"enabled": False, "packages": []}


def test_custom_only_and_disabled_methods(accounts, gateway):
    owner, _ = admin(accounts)
    owner.put(ROOT, json=config(packages=[]))
    catalog = owner.get("/payments/catalog").json()
    assert catalog["enabled"] and catalog["packages"] == [] and catalog["custom"]["credits_per_yuan"] == 250
    methods = [{**m, "enabled": False} for m in config()["payment_methods"]]
    owner.put(ROOT, json=config(version=1, payment_methods=methods))
    assert owner.get("/payments/catalog").json() == {"enabled": False, "packages": []}
