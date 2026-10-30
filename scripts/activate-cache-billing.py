"""Enable the ledger and apply two explicitly confirmed output-unit corrections."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import runpy
from uuid import UUID

from sqlalchemy import select

from app.db import SessionLocal
from app.model_pricing import complete, settings_locked, switch_billing
from app.models import ModelBillingSettings, SiteModelProvider, Workspace
from app.site_audit import record

CORRECTIONS = (
    ("84060463-d21e-440c-ac58-8b6d8f5d1011", "gpt-6-astra", "1001.001", 1),
    ("3999c9f7-c5e2-417b-b99c-34b2be10e7e7", "deepseek-v4.1-flash", "211", 2),
)


def correct_units(db):
    corrected = []
    for identifier, model, credits, version in CORRECTIONS:
        row = db.get(SiteModelProvider, UUID(identifier), with_for_update=True, populate_existing=True)
        assert row is not None and row.model == model, "Confirmed model identity changed"
        price = row.prices["llm_output_tokens"]
        assert Decimal(str(price["credits_per_unit"])) == Decimal(credits), "Confirmed output price changed"
        if price["unit_size"] == 1_000_000:
            continue
        assert price["unit_size"] == 1000 and row.version == version, "Confirmed price version changed"
        prices = deepcopy(row.prices)
        prices["llm_output_tokens"]["unit_size"] = 1_000_000
        assert complete(prices), "Incomplete model pricing"
        row.prices, row.version = prices, row.version + 1
        record(db, None, "models.base_prices_updated", "model_provider", row.id,
            details={"fields": ["llm_output_tokens.unit_size"], "source": "server_console",
                "reason_code": "confirmed_unit_correction", "version": row.version})
        corrected.append({"id": identifier, "model": model, "unit_size": 1_000_000})
    return corrected


def commercial(db):
    settings = db.get(ModelBillingSettings, 1)
    return {"template": settings.template, "version": settings.version,
        "shared": settings.shared_pricing_enabled, "default": str(settings.default_provider_id),
        "prices": [(str(row.id), row.version, row.prices) for row in db.scalars(
            select(SiteModelProvider).order_by(SiteModelProvider.id))],
        "multipliers": [(str(row.id), row.billing_multiplier_percent, row.billing_version)
            for row in db.scalars(select(Workspace).order_by(Workspace.id))]}


snapshot = runpy.run_path(str(Path(__file__).with_name("verify-decimal-billing.py")))["snapshot"]
with SessionLocal() as db:
    settings = settings_locked(db)
    assert settings.pricing_paused and settings.shared_pricing_enabled
    before, prices = snapshot(db)["tables"], deepcopy(commercial(db))
    corrected = correct_units(db)
    expected = deepcopy(prices)
    for correction in corrected:
        for index, (provider_id, version, policies) in enumerate(expected["prices"]):
            if provider_id == correction["id"]:
                policies["llm_output_tokens"]["unit_size"] = 1_000_000
                expected["prices"][index] = (provider_id, version + 1, policies)
    switch_billing(db, "enable")
    db.flush()
    assert snapshot(db)["tables"] == before, "Historical accounting changed"
    assert commercial(db) == expected, "Unexpected commercial configuration change"
    db.commit()
    print(json.dumps({"enabled": True, "historical_accounting_preserved": True,
        "other_commercial_configuration_preserved": True, "shared_pricing": True,
        "confirmed_output_unit_corrections": corrected}))
