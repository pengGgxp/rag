import json
from fractions import Fraction
from math import ceil
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app import payments
from app.config import Settings
from app.models import ModelBillingSettings, SiteAuditEvent, UsagePolicy, User
from test_auth import register
from test_payments import gateway

ROOT = "/site/admin/pricing-calculator"


def administrator(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    return owner, db


def body(**changes):
    return {"capability": "llm_input_tokens", "unit_size": 1_000_000, "cost_yuan": "2",
        "profit_yuan": "3", "overhead_yuan": "0", "payment_fee_percent": "0.1", "multiplier": "1.00", **changes}


def test_permissions_use_live_site_role(accounts, gateway):
    (owner, _, anonymous), db = accounts
    session = register(owner)
    for method, path, payload in [("GET", ROOT, None), ("POST", ROOT + "/quote", body())]:
        assert anonymous.request(method, path, json=payload).status_code == 401
        assert owner.request(method, path, json=payload).status_code == 403
    actor = db.get(User, UUID(session["user"]["id"]))
    actor.is_site_admin = True
    db.commit()
    assert owner.get(ROOT).json()["ready"]
    actor.is_site_admin = False
    db.commit()
    assert owner.post(ROOT + "/quote", json=body()).status_code == 403


def test_quote_conservative_catalog_exact_fee_and_no_writes(accounts, gateway):
    owner, db = administrator(accounts)
    settings, _ = gateway
    settings.recharge_packages_json = json.dumps([
        {"id": "regular", "name": "普通", "amount_fen": 100, "credits": 900},
        {"id": "bonus", "name": "含赠送积分", "amount_fen": 1000, "credits": 10000},
    ])
    before = [db.scalar(select(func.count()).select_from(model)) for model in (UsagePolicy, ModelBillingSettings, SiteAuditEvent)]
    result = owner.post(ROOT + "/quote", json=body()).json()
    assert result["reference_package"]["id"] == "bonus"
    assert result["credits_per_unit"] == 5006
    assert result["unit_size"] == 1_000_000
    assert result["base_price_yuan"] == "5.00600000"
    assert result["effective_fee_yuan"] == "0.00500600"
    assert result["effective_profit_yuan"] == "3.00099400"
    assert result["target_met"] and not result["loss"] and result["unit_rounding"]
    assert before == [db.scalar(select(func.count()).select_from(model)) for model in (UsagePolicy, ModelBillingSettings, SiteAuditEvent)]
    assert "secret" not in str(result) and "merchant" not in str(result)
    # A custom fee affects only the quote, not payment configuration or prices.
    custom = owner.post(ROOT + "/quote", json=body(payment_fee_percent="10", overhead_yuan="1")).json()
    assert custom["credits_per_unit"] == 6667
    assert custom["effective_profit_yuan"] == "3.00030000"


def test_fractional_exchange_ratio_discount_and_rounding_mode(accounts, gateway):
    owner, db = administrator(accounts)
    settings, _ = gateway
    settings.recharge_packages_json = json.dumps([{"id": "odd", "name": "非整数兑换", "amount_fen": 1234, "credits": 567}])
    payload = body(cost_yuan="0.00000001", profit_yuan="0.12345678", payment_fee_percent="0.1234", multiplier="0.01")
    result = owner.post(ROOT + "/quote", json=payload).json()
    exact = (Fraction("0.00000001") + Fraction("0.12345678")) / (1 - Fraction("0.1234") / 100) * Fraction(56700, 1234)
    assert result["credits_per_unit"] == ceil(exact)
    assert not result["target_met"]
    assert owner.post(ROOT + "/quote", json=body(cost_yuan="5", profit_yuan="1", multiplier="0.01")).json()["loss"]
    row = ModelBillingSettings(id=1, enabled=True, template={}, version=0)
    db.add(row); db.commit()
    assert not owner.post(ROOT + "/quote", json=body()).json()["unit_rounding"]
    assert owner.post(ROOT + "/quote", json=body(capability="ocr_pages", unit_size=1)).json()["unit_rounding"]


def test_zero_integer_boundary_and_overflow(accounts, gateway):
    owner, _ = administrator(accounts)
    settings, _ = gateway
    settings.recharge_packages_json = json.dumps([{"id": "one", "name": "整数兑换", "amount_fen": 100, "credits": 1000}])
    assert owner.post(ROOT + "/quote", json=body(cost_yuan="0", profit_yuan="0")).json()["credits_per_unit"] == 0
    assert owner.post(ROOT + "/quote", json=body(cost_yuan="0.001", profit_yuan="0", payment_fee_percent="0")).json()["credits_per_unit"] == 1
    assert owner.post(ROOT + "/quote", json=body(cost_yuan="0.00100001", profit_yuan="0", payment_fee_percent="0")).json()["credits_per_unit"] == 2
    assert owner.post(ROOT + "/quote", json=body(cost_yuan="999999999")).status_code == 422


@pytest.mark.parametrize("patch", [
    {"cost_yuan": "-1"}, {"profit_yuan": "NaN"}, {"overhead_yuan": "1e3"}, {"cost_yuan": True},
    {"profit_yuan": 3}, {"cost_yuan": "0.000000001"}, {"payment_fee_percent": "100"},
    {"payment_fee_percent": "-0.1"}, {"payment_fee_percent": "0.00001"}, {"payment_fee_percent": ""},
    {"unit_size": 0}, {"unit_size": 0.5}, {"unit_size": True}, {"unit_size": 2147483648},
    {"multiplier": "0"}, {"multiplier": "100.01"}, {"multiplier": "1.001"}, {"capability": "mcp_calls"},
    {"credits_per_yuan": "100000"}, {"package_id": "fake"},
])
def test_invalid_inputs_cannot_supply_conversion_or_escape_limits(accounts, gateway, patch):
    owner, _ = administrator(accounts)
    assert owner.post(ROOT + "/quote", json=body(**patch)).status_code == 422


def test_missing_catalog_never_invents_rate(accounts, monkeypatch):
    owner, _ = administrator(accounts)
    settings = Settings(_env_file=None).model_copy(update={"zhifux_enabled": False})
    monkeypatch.setattr(payments, "get_settings", lambda: settings)
    assert owner.get(ROOT).json() == {"ready": False, "reference_package": None,
        "reason": "请先配置可用的充值套餐，再按人民币成本计算价格"}
    assert owner.post(ROOT + "/quote", json=body()).status_code == 503
    settings.zhifux_enabled = True
    settings.recharge_packages_json = "invalid"
    assert not owner.get(ROOT).json()["ready"]
