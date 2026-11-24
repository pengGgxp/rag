from fractions import Fraction
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import model_billing as billing, workspace_billing as pricing
from app.credits import authorization_hold, settle_model_usage, settle_usage
from app.model_runtime.client import LLMUsage
from app.models import (BillingSnapshot, UserCreditAccount, LLMProviderConfig, ModelBillingSettings, ModelUsageRequest,
                        SiteModelProvider, UsageEvent, UsagePolicy, User,
                        UserModelPolicy)
from test_auth import register
from test_shared_providers import setup_site, provider, choose, prices


@pytest.fixture
def shared_billing(accounts, monkeypatch):
    client, db, session = setup_site(accounts, monkeypatch)
    settings = db.get(ModelBillingSettings, 1)
    settings.template = {
        "ocr_pages": {"capability": "ocr_pages", "unit": "pages", "unit_size": 1, "credits_per_unit": 10, "enabled": True},
        "embedding_tokens": {"capability": "embedding_tokens", "unit": "tokens", "unit_size": 1000,
                             "credits_per_unit": 2, "enabled": True}}
    settings.version = 1
    db.commit()
    p = provider(client, rate=1)
    choose(client, p["id"])
    return client, db, UUID(session["user"]["id"]), UUID(session["user"]["id"]), UUID(p["id"])


def binding(db, workspace):
    from app.models import LLMProviderConfig
    return db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.account_id == workspace))


def model(db, workspace):
    row = binding(db, workspace)
    return SimpleNamespace(status=SimpleNamespace(provider_id=str(row.id), provider_key=row.key, model=row.model))


def set_user_multiplier(db, user_id, percent, provider_id=None):
    policy = db.get(UserModelPolicy, user_id)
    if policy is None:
        policy = UserModelPolicy(user_id=user_id)
        db.add(policy)
    policy.billing_multiplier_percent = percent
    if provider_id is not None:
        binding_row = db.get(LLMProviderConfig, provider_id)
        policy.available_models = [str(binding_row.site_provider_id)]
    policy.version = (policy.version or 0) + 1
    db.flush()


def user_account(db, user_id):
    return db.get(UserCreditAccount, user_id)


def test_shared_selection_never_copies_prices_and_switchback_reads_live_price(shared_billing):
    client, db, workspace, _, provider_id = shared_billing
    assert db.scalars(select(UsagePolicy)).all() == []
    row = db.get(SiteModelProvider, provider_id)
    row.prices, row.version = {p["capability"]: p for p in prices(7)}, row.version + 1
    db.commit()
    assert billing.price_snapshot(db, workspace, binding(db, workspace).id)["llm_input_tokens"]["credits_per_unit"] == 7
    choose(client, str(provider_id))
    assert db.scalars(select(UsagePolicy)).all() == []


@pytest.mark.parametrize("percent", [1, 50, 100, 125, 10000])
def test_request_exact_multiplier_applied_once(shared_billing, percent):
    _, db, workspace, user, _ = shared_billing
    set_user_multiplier(db, user, percent, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    for _ in range(3):
        call = billing.begin_call(db, request, model(db, workspace))
        billing.finish_call(db, request, call, LLMUsage(prompt_tokens=150, completion_tokens=150,
            cached_input_tokens=50, cache_status="reported").measured(), "completed")
    total = billing.finalize_request(db, request, "completed")
    db.commit()
    from app.credit_amounts import amount
    assert total == amount(Fraction(9, 10) * Fraction(percent, 100))
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
    assert sum(e.credits for e in events) == total
    assert all(e.policy_snapshot["multiplier_percent"] == percent for e in events)
    assert billing.finalize_request(db, request, "completed") == total


def test_request_base_price_and_multiplier_are_frozen(shared_billing):
    _, db, workspace, user, provider_id = shared_billing
    set_user_multiplier(db, user, 125, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    first = billing.begin_call(db, request, model(db, workspace))
    billing.finish_call(db, request, first, {"input_tokens": 1000, "completion_tokens": 0}, "completed")
    db.get(SiteModelProvider, provider_id).prices = {p["capability"]: p for p in prices(3)}
    db.get(SiteModelProvider, provider_id).version += 1
    db.get(UserModelPolicy, workspace).billing_multiplier_percent = 10000
    db.commit()
    second = billing.begin_call(db, request, model(db, workspace))
    billing.finish_call(db, request, second, {"input_tokens": 1000, "completion_tokens": 0}, "completed")
    assert billing.finalize_request(db, request, "completed") == Decimal("2.5")
    db.commit()
    assert db.get(ModelUsageRequest, request).billing_context["multiplier_percent"] == 125


def test_legacy_engine_uses_actual_units_before_one_request_multiplier(shared_billing):
    _, db, workspace, user, _ = shared_billing
    set_user_multiplier(db, user, 25, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    pricing.operation_snapshot(db, workspace, request, "model")
    authorization_hold(db, workspace, user, request, 1); db.commit()
    meter = pricing.LegacyPriceMeter(sessionmaker(bind=db.get_bind()), workspace, request)
    usages = []
    for _ in range(3):
        meter.invoke(model(db, workspace), lambda: None, [])
        usages.append({"prompt_tokens": 1, "completion_tokens": 1,
            "provider_id": str(binding(db, workspace).id), "billing_snapshot_id": meter.last_snapshot_id})
    assert settle_model_usage(db, workspace, user, request, usages) == Decimal("0.0015")
    db.commit()
    assert user_account(db, user).balance == Decimal("999.9985")
    assert not db.get(ModelBillingSettings, 1).enabled
    assert settle_model_usage(db, workspace, user, request, usages) == Decimal("0.0015")
    assert user_account(db, user).balance == Decimal("999.9985")


@pytest.mark.parametrize("capability,event_key,units,expected", [
    ("ocr_pages", "ocr", 3, Decimal("37.5")), ("embedding_tokens", "embedding", 1001, Decimal("2.5025"))])
def test_document_snapshot_survives_base_and_multiplier_edits(shared_billing, capability, event_key, units, expected):
    _, db, workspace, user, _ = shared_billing
    set_user_multiplier(db, user, 125, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    context = pricing.document_snapshot(db, workspace, request, capability, event_key)
    db.commit()
    db.get(ModelBillingSettings, 1).template = {}
    set_user_multiplier(db, user, 1)
    db.commit()
    restored = pricing.document_snapshot(db, workspace, request, capability, event_key)
    assert restored == context
    event = settle_usage(db, workspace, user, request, capability, units,
        event_key=event_key, pricing_context=restored)
    db.commit()
    assert event.credits == expected
    assert settle_usage(db, workspace, user, request, capability, units, event_key=event_key).id == event.id
    assert user_account(db, user).balance == 1000 - expected


def test_missing_or_disabled_blocks_without_fallback_but_explicit_zero_is_valid(shared_billing):
    _, db, workspace, user, provider_id = shared_billing
    row = db.get(SiteModelProvider, provider_id)
    original = row.prices
    row.prices = {k: v for k, v in original.items() if k != "llm_cached_input_tokens"}
    db.add(UsagePolicy(account_id=workspace, provider_id=binding(db, workspace).id,
        capability="llm_cached_input_tokens", unit="tokens", unit_size=1000, credits_per_unit=0))
    db.commit()
    with pytest.raises(HTTPException):
        billing.start_request(db, workspace, user, uuid4(), binding(db, workspace).id, 1)
    db.rollback()
    row.prices = {p["capability"]: p for p in prices(0)}
    db.commit()
    request = uuid4()
    billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    call = billing.begin_call(db, request, model(db, workspace))
    billing.finish_call(db, request, call, {"input_tokens": 5000, "completion_tokens": 1000}, "completed")
    assert billing.finalize_request(db, request, "completed") == 0
    db.commit()
    assert user_account(db, user).balance == 1000


def test_pending_amount_keeps_original_prices_and_multiplier(shared_billing):
    _, db, workspace, user, provider_id = shared_billing
    set_user_multiplier(db, user, 125, binding(db, workspace).id)
    db.commit()
    request = uuid4()
    billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    call = billing.begin_call(db, request, model(db, workspace))
    billing.finish_call(db, request, call, {"input_tokens": 1000000, "completion_tokens": 0}, "completed")
    assert billing.finalize_request(db, request, "completed") == 1250
    db.commit()
    assert db.get(ModelUsageRequest, request).status == "pending"
    set_user_multiplier(db, user, 1)
    db.get(SiteModelProvider, provider_id).prices = {p["capability"]: p for p in prices(0)}
    user_account(db, user).balance = 1300
    db.commit()
    assert billing.finalize_request(db, request, "completed") == 1250
    billing.start_request(db, workspace, user, uuid4(), binding(db, workspace).id, 1)
    db.commit()
    assert db.get(ModelUsageRequest, request).status == "settled"
    assert user_account(db, user).balance == 49


def test_provider_disable_blocks_next_call_without_losing_confirmed_usage(shared_billing):
    _, db, workspace, user, provider_id = shared_billing
    request = uuid4()
    billing.start_request(db, workspace, user, request, binding(db, workspace).id, 1)
    call = billing.begin_call(db, request, model(db, workspace))
    billing.finish_call(db, request, call, {"input_tokens": 1000, "completion_tokens": 0}, "completed")
    db.get(SiteModelProvider, provider_id).enabled = False
    db.commit()
    with pytest.raises(HTTPException, match="停用"):
        billing.begin_call(db, request, model(db, workspace))
    db.rollback()
    assert billing.finalize_request(db, request, "failed") == 1
    db.commit()
    assert user_account(db, user).balance == 999


def test_maintenance_blocks_new_operations_but_resumes_existing_snapshot(shared_billing):
    _, db, workspace, _, _ = shared_billing
    identifier = uuid4()
    first = pricing.document_snapshot(db, workspace, identifier, "ocr_pages", "ocr")
    db.commit()
    db.get(ModelBillingSettings, 1).pricing_paused = True
    db.commit()
    assert pricing.document_snapshot(db, workspace, identifier, "ocr_pages", "ocr") == first
    with pytest.raises(HTTPException):
        pricing.document_snapshot(db, workspace, uuid4(), "ocr_pages", "ocr")


@pytest.mark.parametrize("value", ["0", "-1", "100.01", "1.001", "", "NaN", "Infinity", 1.25, True, None])
def test_multiplier_api_rejects_invalid_without_changing_config(shared_billing, value):
    client, db, workspace, _, _ = shared_billing
    response = client.put(f"/site/admin/workspaces/{workspace}/billing-settings",
                          json={"multiplier": value, "version": 0})
    assert response.status_code == 404


def test_multiplier_api_site_only_cross_workspace_conflicts_and_audit(shared_billing, accounts):
    client, db, workspace, _, _ = shared_billing
    (_, other, anonymous), _ = accounts
    other_session = register(other, "multiplier-other@example.com")
    target = UUID(other_session["user"]["id"])
    path = f"/site/admin/users/{target}/model-policy"
    payload = {"available_models": [], "default_model": None,
        "billing_multiplier_percent": 125, "version": 0}
    assert other.put(path, json=payload).status_code == 403
    assert anonymous.put(path, json=payload).status_code == 401
    response = client.put(path, json={
        "available_models": [], "default_model": None,
        "billing_multiplier_percent": 125, "version": 0,
    })
    assert response.status_code == 200
    assert response.json()["model_policy"]["billing_multiplier_percent"] == 125
    assert client.put(path, json={
        "available_models": [], "default_model": None,
        "billing_multiplier_percent": 150, "version": 0,
    }).status_code == 409
    from app.models import SiteAuditEvent
    event = db.scalar(select(SiteAuditEvent).where(
        SiteAuditEvent.action == "user.model_policy_changed",
        SiteAuditEvent.target_id == str(target),
    ).order_by(SiteAuditEvent.created_at.desc()))
    assert event and event.details["billing_multiplier_percent"] == 125


def test_migration_requires_reviewed_versions_complete_prices_and_drain(shared_billing):
    from app.billing_admin import migration_command
    client, db, workspace, _, provider_id = shared_billing
    settings = db.get(ModelBillingSettings, 1)
    settings.shared_pricing_enabled = False
    db.commit()
    preview = migration_command(db, "preview")
    assert preview["ready"] and not preview["billing_enabled"]
    assert preview["users"][0]["multiplier"] == "1.00"
    with pytest.raises(ValueError, match="Pause"):
        migration_command(db, "activate", preview["fingerprint"])
    db.rollback()
    migration_command(db, "pause"); db.commit()
    # Pausing does not invalidate reviewed prices.
    assert migration_command(db, "preview")["fingerprint"] == preview["fingerprint"]
    set_user_multiplier(db, workspace, 125, binding(db, workspace).id)
    db.commit()
    with pytest.raises(ValueError, match="fresh preview"):
        migration_command(db, "activate", preview["fingerprint"])
    db.rollback()
    current = migration_command(db, "preview")
    request = BillingSnapshot(account_id=workspace, request_id=uuid4(), event_key="model", context={})
    db.add(request); db.commit()
    with pytest.raises(ValueError, match="Drain"):
        migration_command(db, "activate", current["fingerprint"])
    db.rollback()
    request.status = "complete"
    db.commit()
    assert migration_command(db, "activate", current["fingerprint"])["changed"]
    db.commit()
    assert migration_command(db, "activate", current["fingerprint"])["changed"] is False
    migration_command(db, "resume"); db.commit()
    assert db.get(ModelBillingSettings, 1).shared_pricing_enabled
    assert not db.get(ModelBillingSettings, 1).enabled
    assert not db.get(ModelBillingSettings, 1).pricing_paused


def test_migration_never_autofills_missing_cache_or_overwrites_legacy_prices(shared_billing):
    from app.billing_admin import migration_command
    _, db, workspace, _, provider_id = shared_billing
    settings = db.get(ModelBillingSettings, 1)
    settings.shared_pricing_enabled = False
    old = UsagePolicy(account_id=workspace, capability="ocr_pages", unit="pages",
                      unit_size=1, credits_per_unit=999, version=8)
    db.add(old)
    provider = db.get(SiteModelProvider, provider_id)
    provider.prices = {k: v for k, v in provider.prices.items() if k != "llm_cached_input_tokens"}
    db.commit()
    report = migration_command(db, "preview")
    assert not report["ready"] and report["users"][0]["missing"]
    migration_command(db, "pause"); db.commit()
    with pytest.raises(ValueError, match="missing shared prices"):
        migration_command(db, "activate", report["fingerprint"])
    db.rollback()
    db.refresh(old)
    assert old.credits_per_unit == 999 and old.version == 8
    assert "llm_cached_input_tokens" not in db.get(SiteModelProvider, provider_id).prices
