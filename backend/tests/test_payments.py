import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import payments
from app.auth import now
from app.config import Settings
from app.models import AuditLog, CreditAccount, CreditLedgerEntry, RechargeOrder
from app.payments import CALLBACK_PATH, Checkout, GatewayError, accept_payment, md5_sign
from test_auth import accounts, register, share, switch


@pytest.fixture
def gateway(accounts, monkeypatch):
    settings = Settings(_env_file=None).model_copy(update={
        "zhifux_enabled": True,
        "zhifux_api_base_url": "https://gateway.example/api",
        "zhifux_merchant_num": "888888",
        "zhifux_secret": SecretStr("test-only-secret"),
        "zhifux_pay_type": "aloop",
        "api_public_url": "https://api.example",
        "frontend_url": "https://app.example",
        "recharge_packages_json": json.dumps([
            {"id": "test", "name": "测试档位", "amount_fen": 1234, "credits": 567},
            {"id": "other", "name": "其他档位", "amount_fen": 5000, "credits": 2500},
        ]),
    })
    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    calls = []

    def start(_settings, order):
        calls.append(order.id)
        return Checkout("P" + order.id.hex, "https://checkout.example/pay/" + order.id.hex)

    monkeypatch.setattr(payments, "start_checkout", start)
    return settings, calls


def create(client, *, key=None, package="test"):
    options = client.get("/payments/catalog").json().get("packages", [])
    quote = next((item["quote"] for item in options if item["id"] == package), "0" * 64)
    return client.post("/payments/orders", json={
        "package_id": package, "idempotency_key": key or str(uuid4()), "quote": quote,
    })


def notification(order, settings, **changes):
    result = {
        "state": "1", "merchantNum": settings.zhifux_merchant_num,
        "orderNo": order["order_no"], "amount": "12.34",
        "platformOrderNo": "P" + order["order_no"], "type": "alipay",
        "actualPayAmount": "12.33", "payTime": "2026-10-22 12:34:00", "payee": "测试",
    }
    result.update(changes)
    result["sign"] = md5_sign(
        result["state"] + result["merchantNum"] + result["orderNo"] + result["amount"]
        + settings.zhifux_secret.get_secret_value(),
    )
    return result


def test_catalog_is_disabled_without_complete_config(accounts, monkeypatch):
    (owner, _, anonymous), _db = accounts
    register(owner)
    settings = Settings(_env_file=None).model_copy(update={"zhifux_enabled": False})
    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    assert anonymous.get("/payments/catalog").status_code == 401
    assert owner.get("/payments/catalog").json() == {"enabled": False, "packages": []}
    assert create(owner).status_code == 409
    settings.zhifux_enabled = True
    assert owner.get("/payments/catalog").json() == {"enabled": False, "packages": []}
    assert create(owner).status_code == 503


def test_order_is_immutable_idempotent_and_does_not_credit_before_payment(accounts, gateway):
    (owner, _, _), db = accounts
    register(owner)
    settings, calls = gateway
    catalog = owner.get("/payments/catalog").json()
    assert catalog["enabled"] is True
    assert "test-only-secret" not in str(catalog)
    key = str(uuid4())
    order = create(owner, key=key)
    assert order.status_code == 201, order.text
    first = order.json()
    assert first["status"] == "pending"
    assert first["amount_fen"] == 1234 and first["credits"] == 567
    assert len(first["order_no"]) == 32
    assert create(owner, key=key).json()["id"] == first["id"]
    assert len(calls) == 1
    assert create(owner, key=key, package="other").status_code == 409
    assert owner.get("/usage").json()["balance"] == 1000
    assert owner.get("/payments/orders").json()["total"] == 1
    assert owner.get(f"/payments/orders/{first['id']}").json() == first
    assert owner.get("/payments/orders?page=0").status_code == 422
    assert owner.get("/payments/orders?page_size=9999").status_code == 422

    for extra in ({"amount_fen": 1}, {"credits": 9999999}, {"status": "paid"}):
        assert owner.post("/payments/orders", json={
            "package_id": "test", "idempotency_key": str(uuid4()), **extra,
        }).status_code == 422
    assert create(owner, package="removed").status_code == 409

    settings.recharge_packages_json = "[]"
    settings.zhifux_enabled = False
    # Already quoted orders and successful callbacks survive catalogue changes.
    assert create(owner, key=key).json()["amount_fen"] == 1234
    paid = owner.get(CALLBACK_PATH, params=notification(first, settings))
    assert paid.status_code == 200 and paid.text == "success"
    assert owner.get("/usage").json()["balance"] == 1567
    assert db.get(RechargeOrder, UUID(first["id"])).status == "paid"


def test_callbacks_are_public_signed_and_idempotent(accounts, gateway):
    (owner, _, anonymous), db = accounts
    register(owner)
    settings, _ = gateway
    order = create(owner).json()
    anonymous.headers.clear()
    data = notification(order, settings)
    assert anonymous.get(CALLBACK_PATH, params=data).text == "success"
    assert anonymous.post(CALLBACK_PATH, data=data).text == "success"
    assert anonymous.get(CALLBACK_PATH, params=data).text == "success"
    assert owner.get("/usage").json()["balance"] == 1567
    entries = db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "paid_recharge")).all()
    assert len(entries) == 1 and entries[0].amount == 567
    assert entries[0].metadata_json["amount_fen"] == 1234
    assert len(db.scalars(select(AuditLog).where(AuditLog.action == "recharge.paid")).all()) == 1
    paid = owner.get(f"/payments/orders/{order['id']}").json()
    assert paid["status"] == "paid" and paid["pay_url"] is None and paid["paid_at"]
    # Only the callback path is exempt from browser source validation.
    assert anonymous.post("/payments/orders", json={}).status_code == 403
    assert anonymous.post("/auth/register", json={}).status_code == 403


@pytest.mark.parametrize("changes", [
    {"state": "0"}, {"merchantNum": "other"}, {"orderNo": uuid4().hex},
    {"amount": "0.01"}, {"amount": "1234"}, {"amount": "-12.34"},
    {"amount": "12.340"}, {"amount": "1.234e1"}, {"amount": "NaN"},
    {"platformOrderNo": "WrongPlatformId"},
])
def test_invalid_notifications_never_post_credits(accounts, gateway, changes):
    (owner, _, anonymous), db = accounts
    register(owner)
    settings, _ = gateway
    order = create(owner).json()
    data = notification(order, settings, **changes)
    result = anonymous.get(CALLBACK_PATH, params=data)
    assert result.status_code == 400 and result.text == "fail"
    assert owner.get("/usage").json()["balance"] == 1000
    assert db.get(RechargeOrder, UUID(order["id"])).status == "pending"


def test_bad_signature_and_ambiguous_callback_parameters_are_rejected(accounts, gateway):
    (owner, _, anonymous), _ = accounts
    register(owner)
    settings, _ = gateway
    order = create(owner).json()
    data = notification(order, settings)
    bad_sign = {**data, "sign": "0" * 32}
    assert anonymous.get(CALLBACK_PATH, params=bad_sign).text == "fail"
    assert anonymous.get(CALLBACK_PATH, params={**data, "sign": "你好"}).text == "fail"
    assert anonymous.get(CALLBACK_PATH, params=list(data.items()) + [("amount", "0.01")]).text == "fail"
    assert anonymous.post(CALLBACK_PATH + "?state=1", data=data).text == "fail"
    assert anonymous.post(CALLBACK_PATH, json=data).text == "fail"
    assert anonymous.post(CALLBACK_PATH, content=b"x" * 8193,
                          headers={"content-type": "application/x-www-form-urlencoded"}).text == "fail"
    assert anonymous.get(CALLBACK_PATH + "?x=" + "x" * 8193).text == "fail"
    assert owner.get("/usage").json()["balance"] == 1000


def test_signature_uses_raw_amount_and_not_floating_actual_amount(accounts, gateway):
    (owner, _, _), _db = accounts
    register(owner)
    settings, _ = gateway
    order = create(owner, package="other").json()
    data = notification(order, settings, amount="50", actualPayAmount="0.01")
    assert owner.post(CALLBACK_PATH, data=data).text == "success"
    assert owner.get("/usage").json()["balance"] == 3500


def test_orders_are_private_even_to_other_workspace_admins(accounts, gateway):
    (owner, other, anonymous), _ = accounts
    first = register(owner)
    second = register(other, "other@example.com")
    order = create(owner).json()
    assert anonymous.get(f"/payments/orders/{order['id']}").status_code == 401
    assert other.get(f"/payments/orders/{order['id']}").status_code == 404
    share(owner, "other@example.com", "admin")
    switch(other, first["workspace_id"])
    assert other.get(f"/payments/orders/{order['id']}").status_code == 404
    assert other.get("/payments/orders").json()["items"] == []
    # A member can fund shared credits, but cannot read another user's orders.
    own_order = create(other).json()
    assert own_order["id"] != order["id"]
    assert owner.get(f"/payments/orders/{own_order['id']}").status_code == 404
    switch(other, second["workspace_id"])
    assert other.get(f"/payments/orders/{own_order['id']}").status_code == 404


@pytest.mark.parametrize("rejected, expected", [(False, "confirming"), (True, "failed")])
def test_gateway_failures_are_durable_and_are_not_automatically_retried(accounts, gateway, monkeypatch, rejected, expected):
    (owner, _, _), _db = accounts
    register(owner)
    calls = []

    def fail(*_):
        calls.append(True)
        raise GatewayError(rejected=rejected)

    monkeypatch.setattr(payments, "start_checkout", fail)
    key = str(uuid4())
    first = create(owner, key=key)
    assert first.status_code == 201 and first.json()["status"] == expected
    assert create(owner, key=key).json()["id"] == first.json()["id"]
    assert len(calls) == 1
    assert owner.get("/usage").json()["balance"] == 1000
    settings, _ = gateway
    assert owner.get(CALLBACK_PATH, params=notification(first.json(), settings)).text == "success"
    assert owner.get("/usage").json()["balance"] == 1567


def test_expired_order_still_accepts_late_payment_without_exposing_old_checkout(accounts, gateway):
    (owner, _, _), db = accounts
    register(owner)
    settings, _ = gateway
    order = create(owner).json()
    row = db.get(RechargeOrder, UUID(order["id"]))
    row.expires_at = now() - timedelta(seconds=1)
    db.commit()
    expired = owner.get(f"/payments/orders/{order['id']}").json()
    assert expired["status"] == "expired" and expired["pay_url"] is None
    assert owner.get(CALLBACK_PATH, params=notification(order, settings)).text == "success"
    assert owner.get("/usage").json()["balance"] == 1567


def test_callback_before_creation_response_is_not_overwritten(accounts, gateway, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    settings, _ = gateway

    def early_callback(_settings, row):
        data = notification({"order_no": row.id.hex}, settings)
        accept_payment(db, data, settings)
        return Checkout(data["platformOrderNo"], "https://checkout.example/pay")

    monkeypatch.setattr(payments, "start_checkout", early_callback)
    order = create(owner).json()
    assert order["status"] == "paid" and order["pay_url"] is None
    assert owner.get("/usage").json()["balance"] == 1567


def test_transaction_rolls_back_all_posting_on_audit_failure(accounts, gateway, monkeypatch):
    (owner, _, _), db = accounts
    session = register(owner)
    settings, _ = gateway
    order = create(owner).json()

    def fail(*_args, **_kwargs):
        raise RuntimeError("test audit failed")

    monkeypatch.setattr(payments, "record_audit", fail)
    with pytest.raises(RuntimeError):
        accept_payment(db, notification(order, settings), settings)
    db.rollback()
    assert db.get(CreditAccount, UUID(session["workspace_id"])).balance == 1000
    assert db.get(RechargeOrder, UUID(order["id"])).status == "pending"
    assert db.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "paid_recharge")) is None


def test_platform_order_id_cannot_be_used_for_two_orders(accounts, gateway):
    (owner, _, _), _db = accounts
    register(owner)
    settings, _ = gateway
    first = create(owner).json()
    second = create(owner).json()
    assert owner.get(CALLBACK_PATH, params=notification(first, settings)).text == "success"
    assert owner.get(CALLBACK_PATH, params=notification(
        second, settings, platformOrderNo="P" + first["order_no"],
    )).text == "fail"
    assert owner.get("/usage").json()["balance"] == 1567


@pytest.mark.parametrize("setting,value", [
    ("zhifux_api_base_url", "http://gateway.example"),
    ("api_public_url", "https://api.example?callback=bad"),
    ("frontend_url", "javascript:alert(1)"),
    ("zhifux_merchant_num", ""),
    ("recharge_packages_json", "[]"),
    ("recharge_packages_json", '[{"id":"a","name":"a","amount_fen":0,"credits":1}]'),
    ("recharge_packages_json", '[{"id":"a","name":"a","amount_fen":1.1,"credits":1}]'),
    ("recharge_packages_json", '[{"id":"a","name":"a","amount_fen":1,"credits":true}]'),
])
def test_bad_configuration_fails_closed(accounts, gateway, setting, value):
    (owner, _, _), _ = accounts
    register(owner)
    settings, calls = gateway
    setattr(settings, setting, value)
    assert owner.get("/payments/catalog").json() == {"enabled": False, "packages": []}
    assert create(owner).status_code == 503
    assert calls == []


def test_payment_return_parameters_cannot_credit_account(accounts, gateway):
    (owner, _, _), _ = accounts
    register(owner)
    order = create(owner).json()
    response = owner.get("/payments/orders", params={
        "orderState": "1", "orderNo": order["order_no"], "amount": "12.34",
    })
    assert response.json()["items"][0]["status"] == "pending"
    assert owner.get("/usage").json()["balance"] == 1000


def test_price_change_requires_a_new_customer_confirmation(accounts, gateway):
    (owner, _, _), _db = accounts
    register(owner)
    settings, calls = gateway
    quote = owner.get("/payments/catalog").json()["packages"][0]["quote"]
    packages = json.loads(settings.recharge_packages_json)
    packages[0]["amount_fen"] = 2000
    settings.recharge_packages_json = json.dumps(packages)
    response = owner.post("/payments/orders", json={
        "package_id": "test", "idempotency_key": uuid4().hex, "quote": quote,
    })
    assert response.status_code == 409
    assert owner.get("/payments/orders").json()["total"] == 0 and calls == []
    assert create(owner).json()["amount_fen"] == 2000


def custom_order(client, amount_fen, *, key=None, quote=None):
    policy = client.get("/payments/catalog").json()["custom"]
    return client.post("/payments/orders", json={
        "package_id": policy["id"], "custom_amount_fen": amount_fen,
        "idempotency_key": key or uuid4().hex, "quote": quote or policy["quote"],
    })


def test_presets_preserve_existing_packages_and_conversion(accounts, gateway):
    (owner, _, _), _ = accounts
    register(owner)
    settings, _ = gateway
    settings.recharge_packages_json = json.dumps([
        {"id": "starter", "name": "基础包", "amount_fen": 1000, "credits": 1000},
    ])
    catalog = owner.get("/payments/catalog").json()
    assert [(p["amount_fen"], p["credits"]) for p in catalog["packages"]] == [
        (1000, 1000), (2000, 2000), (5000, 5000), (10000, 10000), (20000, 20000),
    ]
    assert catalog["packages"][0]["id"] == "starter"
    for fen in (2000, 5000, 10000, 20000):
        response = create(owner, package=f"amount_{fen}")
        assert response.status_code == 201
        assert response.json()["amount_fen"] == response.json()["credits"] == fen


def test_custom_amount_is_quoted_idempotent_and_paid_once(accounts, gateway):
    (owner, _, anonymous), db = accounts
    register(owner)
    settings, calls = gateway
    key = uuid4().hex
    response = custom_order(owner, 2345, key=key)
    assert response.status_code == 201
    order = response.json()
    assert order["amount_fen"] == 2345
    assert order["credits"] == 2345 * 567 // 1234  # no new volume discount
    assert custom_order(owner, 2345, key=key).json()["id"] == order["id"]
    assert custom_order(owner, 2346, key=key).status_code == 409
    assert len(calls) == 1
    assert owner.get("/usage").json()["balance"] == 1000
    data = notification(order, settings, amount="23.45")
    assert anonymous.get(CALLBACK_PATH, params=data).text == "success"
    assert anonymous.get(CALLBACK_PATH, params=data).text == "success"
    assert owner.get("/usage").json()["balance"] == 1000 + order["credits"]
    entries = db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "paid_recharge")).all()
    assert len(entries) == 1 and entries[0].amount == order["credits"]
    settings.zhifux_enabled = False
    # A retry recovers its original order even after the catalogue is disabled.
    assert custom_order_payload(owner, order["amount_fen"], key).json()["id"] == order["id"]


def custom_order_payload(client, amount_fen, key):
    return client.post("/payments/orders", json={"package_id": "custom_amount",
        "custom_amount_fen": amount_fen, "idempotency_key": key, "quote": "0" * 64})


@pytest.mark.parametrize("value", [0, 99, 1_000_001, -100, True, 2345.1, "2345", None])
def test_custom_invalid_amount_never_starts_payment(accounts, gateway, value):
    (owner, _, _), _ = accounts
    register(owner)
    _, calls = gateway
    assert custom_order(owner, value).status_code == 422
    assert not calls
    assert owner.get("/payments/orders").json()["total"] == 0


def test_custom_quote_changes_and_payload_tampering_fail_before_checkout(accounts, gateway):
    (owner, _, _), _ = accounts
    register(owner)
    settings, calls = gateway
    policy = owner.get("/payments/catalog").json()["custom"]
    packages = json.loads(settings.recharge_packages_json)
    packages[0]["credits"] += 1
    settings.recharge_packages_json = json.dumps(packages)
    assert custom_order(owner, 2345, quote=policy["quote"]).status_code == 409
    for changes in ({"credits": 999999}, {"amount_fen": 1}, {"custom_amount_fen": 2000}):
        assert owner.post("/payments/orders", json={"package_id": "test",
            "idempotency_key": uuid4().hex, "quote": "0" * 64, **changes}).status_code == 422
    assert not calls


def test_catalog_avoids_configured_id_collisions_and_keeps_discounts(accounts, gateway):
    (owner, _, _), _ = accounts
    register(owner)
    settings, _ = gateway
    settings.recharge_packages_json = json.dumps([
        {"id": "custom_amount", "name": "原套餐", "amount_fen": 1000, "credits": 1000},
        {"id": "amount_2000", "name": "优惠套餐", "amount_fen": 5000, "credits": 6000},
    ])
    catalog = owner.get("/payments/catalog").json()
    assert len({p["id"] for p in catalog["packages"]}) == len(catalog["packages"])
    assert catalog["custom"]["id"] == "custom_amount_"
    assert next(p for p in catalog["packages"] if p["amount_fen"] == 5000)["credits"] == 6000
    assert custom_order(owner, 5000).json()["credits"] == 5000


def test_custom_limits_avoid_zero_or_excessive_integer_credits(accounts, gateway):
    (owner, _, _), _ = accounts
    register(owner)
    settings, calls = gateway
    settings.recharge_packages_json = json.dumps([
        {"id": "one", "name": "高比例", "amount_fen": 100, "credits": 1000000},
    ])
    catalog = owner.get("/payments/catalog").json()
    assert catalog["custom"]["max_amount_fen"] == 100
    assert custom_order(owner, 101).status_code == 422
    assert custom_order(owner, 100).json()["credits"] == 1000000
    assert len(calls) == 1


def test_real_adapter_sends_official_query_protocol_with_tls_and_no_secret_exposure(accounts, gateway, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    settings, _ = gateway
    order_json = create(owner).json()
    row = db.get(RechargeOrder, UUID(order_json["id"]))
    original_client = httpx.Client
    observed = []

    def handle(request):
        observed.append(request)
        params = request.url.params
        assert request.method == "POST" and request.url.path == "/api/startOrder"
        assert request.content == b""
        assert params["amount"] == "12.34"
        assert params["apiMode"] == "post_form" and params["payDuration"] == "15"
        assert params["returnType"] == "json"
        assert params["returnUrl"] == "https://app.example/?account=1&billing=1"
        assert params["sign"] == md5_sign(
            settings.zhifux_merchant_num + row.id.hex + "12.34" + "https://api.example" + CALLBACK_PATH + "test-only-secret",
        )
        assert "secret" not in params
        return httpx.Response(200, json={
            "success": True, "code": 200, "data": {"id": "12345", "payUrl": "https://checkout.example/pay"},
        })

    monkeypatch.setattr(payments.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handle),
    ))
    # Fixture replaced the callable; restore the adapter from its original module definition below.
    checkout = ORIGINAL_START_CHECKOUT(settings, row)
    assert checkout == Checkout("12345", "https://checkout.example/pay")
    assert len(observed) == 1
    assert md5_sign("abc123456") == "0659c7992e268962384eb17fafe88364"


ORIGINAL_START_CHECKOUT = payments.start_checkout


@pytest.mark.parametrize("status,payload,rejected", [
    (400, {"success": False, "msg": "签名不正确"}, True),
    (200, {"success": True, "data": {"id": "1", "payUrl": "http://checkout.example"}}, False),
    (200, {"success": True, "data": {"id": "1", "payUrl": "javascript:alert(1)"}}, False),
    (200, {"success": True, "data": {"id": "1", "payUrl": "https://user:pass@checkout.example"}}, False),
    (302, {"success": True}, False),
    (200, {"success": True, "data": None}, False),
    (200, {"success": True, "data": {"payUrl": "https://checkout.example"}}, False),
    (200, [], False),
])
def test_adapter_rejects_untrusted_gateway_responses(accounts, gateway, monkeypatch, status, payload, rejected):
    (owner, _, _), db = accounts
    register(owner)
    settings, _ = gateway
    row = db.get(RechargeOrder, UUID(create(owner).json()["id"]))
    original_client = httpx.Client
    monkeypatch.setattr(payments.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)),
    ))
    with pytest.raises(GatewayError) as error:
        ORIGINAL_START_CHECKOUT(settings, row)
    assert error.value.rejected == rejected


def test_adapter_timeout_is_uncertain_not_paid(accounts, gateway, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    settings, _ = gateway
    row = db.get(RechargeOrder, UUID(create(owner).json()["id"]))
    original_client = httpx.Client

    def timeout(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    monkeypatch.setattr(payments.httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(timeout),
    ))
    with pytest.raises(GatewayError) as error:
        ORIGINAL_START_CHECKOUT(settings, row)
    assert error.value.rejected is False
