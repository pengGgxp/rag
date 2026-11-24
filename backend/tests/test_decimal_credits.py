from decimal import Decimal
from fractions import Fraction
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.billing import model_usage as model_billing, workspace as workspace_billing
from app.billing.amounts import allocate, amount
from app.billing.credits import authorization_hold, recharge, settle_model_usage, settle_usage
from app.models import UserCreditAccount, ModelBillingSettings, ModelUsageRequest, SiteModelProvider, UsageEvent, UserModelPolicy
from test_workspace_billing import shared_billing, binding, model, set_user_multiplier


@pytest.mark.parametrize("exact,expected", [
    ("1.42096", "1.4210"), ("0.000049", "0.0000"), ("0.00005", "0.0001"),
    ("0.00015", "0.0002"), ("2.5", "2.5000"), ("5206", "5206.0000"),
])
def test_four_decimal_half_up_never_ceil_to_whole_credit(exact, expected):
    assert amount(Fraction(exact)) == Decimal(expected)


def test_allocate_rounds_request_once_and_preserves_sum():
    total, parts = allocate([Fraction(1, 30000)] * 3)
    assert total == Decimal("0.0001")
    assert parts == [Decimal("0.0001"), 0, 0]
    assert sum(parts) == total
    assert allocate([]) == (0, [])
    assert allocate([Fraction(1, 100000)] * 2) == (0, [0, 0])


def test_live_reported_example_and_replay_keep_four_decimal_balance(shared_billing):
    client, db, workspace, user, identifier = shared_billing
    provider = db.get(SiteModelProvider, identifier)
    provider.prices = {key: {"capability": key, "unit": "tokens", "unit_size": 1000000,
        "credits_per_unit": rate, "enabled": True} for key, rate in (
        ("llm_input_tokens", 1402), ("llm_cached_input_tokens", 51), ("llm_output_tokens", 3804))}
    db.commit()
    request = uuid4()
    root = workspace_billing.operation_snapshot(db, workspace, request, "model",
        capabilities=model_billing.CAPABILITIES, provider_id=binding(db, workspace).id)
    call = workspace_billing.operation_snapshot(db, workspace, request, "model:0",
        capabilities=model_billing.CAPABILITIES, provider_id=binding(db, workspace).id, context=root.context)
    authorization_hold(db, workspace, user, request, 1)
    usage = [{"prompt_tokens": 886, "completion_tokens": 47, "reasoning_tokens": 42,
        "source": "provider", "provider_id": str(binding(db, workspace).id), "billing_snapshot_id": str(call.id)}]
    assert settle_model_usage(db, workspace, user, request, usage) == Decimal("1.4210")
    db.commit()
    assert settle_model_usage(db, workspace, user, request, usage) == Decimal("1.4210")
    assert db.get(UserCreditAccount, workspace).balance == Decimal("998.5790")
    assert db.get(UserCreditAccount, workspace).lifetime_spent == Decimal("1.4210")
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
    assert [e.credits for e in sorted(events, key=lambda e: e.capability)] == [Decimal("1.2422"), Decimal("0.1788")]
    read = client.get("/usage").json()
    assert read["balance"] == 998.579 and read["usage"]["units"]["llm_input_tokens"]["credits"] == 1.2422
    assert not db.get(ModelBillingSettings, 1).enabled
    recharge(db, workspace, user, 10, uuid4(), 100)
    db.commit()
    assert db.get(UserCreditAccount, workspace).balance == Decimal("1008.5790")


@pytest.mark.parametrize("capability,units,expected", [
    ("embedding_tokens", 2929, "0.2958"), ("embedding_tokens", 0, "0.0000"),
    ("ocr_pages", 6, "24.0000"),
])
def test_document_actual_units_and_price_snapshot(shared_billing, capability, units, expected):
    _, db, workspace, user, _ = shared_billing
    settings = db.get(ModelBillingSettings, 1)
    settings.template = {key: {**price, "unit_size": 1 if key == "ocr_pages" else 1000000,
        "credits_per_unit": 4 if key == "ocr_pages" else 101} for key, price in settings.template.items()}
    db.commit()
    request = uuid4()
    snapshot = workspace_billing.document_snapshot(db, workspace, request, capability, capability)
    event = settle_usage(db, workspace, user, request, capability, units, pricing_context=snapshot)
    db.commit()
    assert event.credits == Decimal(expected)
    assert settle_usage(db, workspace, user, request, capability, units).id == event.id


def test_fractional_pending_is_not_truncated_and_is_paid_once(shared_billing):
    client, db, workspace, user, _ = shared_billing
    account = db.get(UserCreditAccount, workspace)
    account.balance = 1
    db.commit()
    request = uuid4()
    model_billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    call = model_billing.begin_call(db, request, model(db, workspace))
    model_billing.finish_call(db, request, call, {"input_tokens": 1421, "completion_tokens": 0}, "completed")
    assert model_billing.finalize_request(db, request, "completed") == Decimal("1.4210")
    db.commit()
    assert db.get(ModelUsageRequest, request).status == "pending"
    assert client.get("/usage").json()["pending_credits"] == 1.421
    recharge(db, workspace, user, 1, uuid4(), 100)
    db.commit()
    model_billing.settle_pending(db, workspace)
    db.commit()
    model_billing.settle_pending(db, workspace)
    assert account.balance == Decimal("0.5790")


def test_fractional_price_form_and_multiplier(shared_billing):
    client, db, workspace, _, identifier = shared_billing
    detail = client.get(f"/site/admin/llm-providers/{identifier}").json()
    from test_shared_providers import prices
    policies = prices(1)
    for policy in policies:
        policy["credits_per_unit"] = 0.1234
    result = client.put(f"/site/admin/llm-providers/{identifier}/pricing",
                        json={"version": detail["version"], "policies": policies})
    assert result.status_code == 200, result.text
    assert db.get(SiteModelProvider, identifier).prices["llm_input_tokens"]["credits_per_unit"] == 0.1234
    policies[0]["credits_per_unit"] = 0.12345
    assert client.put(f"/site/admin/llm-providers/{identifier}/pricing",
        json={"version": result.json()["version"], "policies": policies}).status_code == 422
    assert workspace_billing.context_for_workspace(db, workspace)["multiplier_percent"] == 100


def test_document_can_spend_exact_balance_after_hold_refund(shared_billing):
    _, db, workspace, user, _ = shared_billing
    account = db.get(UserCreditAccount, workspace)
    account.balance = Decimal("2.5000")
    request = uuid4()
    authorization_hold(db, workspace, user, request, 1)
    event = settle_usage(db, workspace, user, request, "embedding_tokens", 1250)
    db.commit()
    assert event.credits == Decimal("2.5000")
    assert account.balance == 0
    assert settle_usage(db, workspace, user, request, "embedding_tokens", 1250).id == event.id
    assert account.balance == 0


@pytest.mark.parametrize("cached", [0, 700, 1000])
def test_cache_request_uses_three_rates_frozen_multiplier_and_one_rounding(shared_billing, cached):
    from app.model_runtime.client import _parse_usage
    from app.models import CreditLedgerEntry, ModelCallUsage
    _, db, workspace, user, identifier = shared_billing
    provider = db.get(SiteModelProvider, identifier)
    provider.prices = {key: {"capability": key, "unit": "tokens", "unit_size": 1000000,
        "credits_per_unit": rate, "enabled": True} for key, rate in (
        ("llm_input_tokens", 1402), ("llm_cached_input_tokens", 51), ("llm_output_tokens", 3804))}
    set_user_multiplier(db, workspace, 125, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    model_billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    db.commit()
    for _ in range(2):
        call = model_billing.begin_call(db, request, model(db, workspace))
        usage = _parse_usage({"prompt_tokens": 1000, "completion_tokens": 17,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": 10}}).measured()
        model_billing.finish_call(db, request, call, usage, "completed")
        provider.prices = {key: {**price, "credits_per_unit": 99999} for key, price in provider.prices.items()}
        db.get(UserModelPolicy, workspace).billing_multiplier_percent = 10000
        db.commit()
    exact = Fraction(2 * ((1000-cached)*1402 + cached*51 + 17*3804), 1000000) * Fraction(125, 100)
    assert model_billing.finalize_request(db, request, "cancelled") == amount(exact)
    db.commit()
    assert model_billing.finalize_request(db, request, "cancelled") == amount(exact)
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
    assert len(events) == 6 and sum(row.credits for row in events) == amount(exact)
    assert sum(row.units for row in events if row.capability == "llm_input_tokens") == 2*(1000-cached)
    assert sum(row.units for row in events if row.capability == "llm_cached_input_tokens") == 2*cached
    assert db.get(UserCreditAccount, workspace).balance == 1000-amount(exact)
    assert len(db.scalars(select(CreditLedgerEntry).where(
        CreditLedgerEntry.account_id == workspace, CreditLedgerEntry.kind == "usage_settlement")).all()) == 1
    assert len(db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == request)).all()) == 2
