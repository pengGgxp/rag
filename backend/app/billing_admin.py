"""Site-only multiplier management and versioned, read-only migration previews."""
import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import get_db
from .management_access import ManagementAccess, management_access
from .model_pricing import settings_locked
from .models import (BillingSnapshot, Conversation, IngestionJob, JobState, LLMProviderConfig,
                     ModelUsageRequest, SiteModelProvider, UsagePolicy, Workspace)
from .site_auth import site_admin
from .site_audit import record
from .workspace_billing import MODEL_CAPABILITIES, DOCUMENT_CAPABILITIES, resolve_prices

router = APIRouter(tags=["site-admin"])
ROOT = "/site/admin/workspaces/{workspace_id}/billing-settings"


class MultiplierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    multiplier: str = Field(pattern=r"^(?:0\.(?:0[1-9]|[1-9][0-9]?)|[1-9][0-9]?(?:\.[0-9]{1,2})?|100(?:\.0{1,2})?)$")


def display_rate(percent):
    return f"{percent // 100}.{percent % 100:02d}"


def price_read(price, percent):
    configured = price["configured"]
    base = price.get("credits_per_unit")
    effective = format(Decimal(base) * Decimal(percent) / Decimal(100), "f") if configured else None
    return {**price, "effective_credits_per_unit": effective,
            "available": configured and price.get("enabled", True)}


def effective_prices(db, workspace, binding, shared=True):
    context = {"source_mode": "shared" if shared else "legacy", "multiplier_percent": workspace.billing_multiplier_percent}
    result = list(resolve_prices(db, workspace.id, DOCUMENT_CAPABILITIES, context=context, required=False).values())
    if binding is not None:
        try:
            model = resolve_prices(db, workspace.id, MODEL_CAPABILITIES, binding.id, context=context, required=False)
            result.extend(model.values())
        except HTTPException:
            result.extend({"capability": key, "unit": "tokens", "unit_size": 1000,
                "credits_per_unit": None, "enabled": False, "version": 0, "configured": False,
                "source": "model_provider", "source_id": str(binding.site_provider_id) if binding.site_provider_id else None,
                "provider_id": str(binding.id)} for key in MODEL_CAPABILITIES)
    percent = workspace.billing_multiplier_percent if shared else 100
    return [price_read(price, percent) for price in result]


def settings_read(db, workspace_id):
    from .models import ModelBillingSettings
    workspace = db.get(Workspace, workspace_id)
    row = db.get(ModelBillingSettings, 1)
    bindings = db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id)
                         .order_by(LLMProviderConfig.id)).all()
    models = []
    for binding in bindings:
        provider = db.get(SiteModelProvider, binding.site_provider_id) if binding.site_provider_id else None
        prices = effective_prices(db, workspace, binding)
        models.append({"binding_id": str(binding.id), "provider_id": str(binding.site_provider_id) if binding.site_provider_id else None,
            "name": provider.name if provider else binding.name, "model": provider.model if provider else binding.model,
            "active": binding.active, "enabled": bool(provider and provider.enabled),
            "policies": [p for p in prices if p["capability"] in MODEL_CAPABILITIES]})
    return {"workspace_id": str(workspace_id), "version": workspace.billing_version,
            "multiplier": display_rate(workspace.billing_multiplier_percent),
            "multiplier_percent": workspace.billing_multiplier_percent,
            "shared_pricing_enabled": bool(row and row.shared_pricing_enabled),
            "billing_enabled": bool(row and row.enabled),
            "document_policies": effective_prices(db, workspace, None), "models": models}


@router.get(ROOT)
def read_settings(db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    return settings_read(db, access.workspace_id)


@router.put(ROOT)
def update_settings(payload: MultiplierUpdate, db=Depends(get_db), access: ManagementAccess = Depends(management_access)):
    settings_locked(db)
    workspace = db.get(Workspace, access.workspace_id)
    if workspace.billing_version != payload.version:
        raise HTTPException(409, "工作区倍率已更新，请重新加载后保存")
    previous = workspace.billing_multiplier_percent
    workspace.billing_multiplier_percent = int(Decimal(payload.multiplier) * 100)
    workspace.billing_version += 1
    record(db, access.actor, "billing.workspace_multiplier_updated", "workspace", workspace.id,
        request=access.request, details={"fields": ["multiplier"], "previous_percent": previous,
            "multiplier_percent": workspace.billing_multiplier_percent, "version": workspace.billing_version})
    db.commit()
    return settings_read(db, workspace.id)


def sample(prices, capability, units, percent, precise):
    price = next((p for p in prices if p["capability"] == capability), None)
    if not price or not price["configured"] or not price.get("enabled", True):
        return None
    if precise:
        return Fraction(units * price["credits_per_unit"] * percent, price["unit_size"] * 100)
    base = ((units + price["unit_size"] - 1) // price["unit_size"]) * price["credits_per_unit"]
    return Fraction(base * percent, 100)


def migration_preview(db):
    from .models import ModelBillingSettings
    settings = db.get(ModelBillingSettings, 1, populate_existing=True) or ModelBillingSettings(
        enabled=False, shared_pricing_enabled=False, pricing_paused=False, template={}, version=0)
    workspaces, fingerprint = [], {"template": settings.template, "version": settings.version,
        "settlement": settings.enabled, "shared": settings.shared_pricing_enabled}
    fingerprint["providers"] = [{"id": str(p.id), "version": p.version, "enabled": p.enabled, "prices": p.prices}
        for p in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.id))]
    fingerprint["policies"] = [{"id": str(p.id), "workspace_id": str(p.workspace_id),
        "provider_id": str(p.provider_id) if p.provider_id else None, "capability": p.capability,
        "unit_size": p.unit_size, "credits_per_unit": p.credits_per_unit, "enabled": p.enabled, "version": p.version}
        for p in db.scalars(select(UsagePolicy).order_by(UsagePolicy.id))]
    for workspace in db.scalars(select(Workspace).order_by(Workspace.id)):
        bindings = db.scalars(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace.id)
                             .order_by(LLMProviderConfig.id)).all()
        active = next((b for b in bindings if b.active), None)
        old = effective_prices(db, workspace, active, shared=False)
        new = effective_prices(db, workspace, active)
        by_key = {p["capability"]: p for p in old}
        ratios, changed = set(), False
        for price in new:
            previous = by_key.get(price["capability"])
            if previous and previous["configured"] and price["configured"]:
                old_unit = Fraction(previous["credits_per_unit"], previous["unit_size"])
                new_unit = Fraction(price["credits_per_unit"], price["unit_size"])
                if new_unit:
                    ratios.add(old_unit / new_unit)
                elif old_unit:
                    changed = True
        comparisons = []
        input_units = 1500 if settings.enabled else 2100
        sample_groups = [("模型：两轮各 2100 总输入（含 600 缓存）/ 600 输出",
            [("llm_input_tokens", input_units), ("llm_cached_input_tokens", 600), ("llm_output_tokens", 600)] * 2),
            ("OCR：3 页", [("ocr_pages", 3)]), ("Embedding：1500 Token", [("embedding_tokens", 1500)])]
        for name, items in sample_groups:
            precise = settings.enabled and items[0][0] in MODEL_CAPABILITIES
            applicable = items if precise else [(key, units) for key, units in items if key != "llm_cached_input_tokens"]
            old_parts = [sample(old, key, units, 100, precise) for key, units in applicable]
            new_parts = [sample(new, key, units, workspace.billing_multiplier_percent, precise) for key, units in applicable]
            old_total = ceil(sum(old_parts, Fraction())) if all(p is not None for p in old_parts) else None
            new_total = ceil(sum(new_parts, Fraction())) if all(p is not None for p in new_parts) else None
            comparisons.append({"label": name, "old_credits": old_total, "new_credits": new_total,
                "difference": new_total - old_total if old_total is not None and new_total is not None else None})
        missing = [p["capability"] for p in new if not p["available"]]
        # All associated, enabled providers must be ready, not just the active one.
        for b in bindings:
            provider = db.get(SiteModelProvider, b.site_provider_id) if b.site_provider_id else None
            if provider and provider.enabled:
                missing.extend(f"{b.id}:{p['capability']}" for p in effective_prices(db, workspace, b)
                               if p["capability"] in MODEL_CAPABILITIES and not p["available"])
        workspaces.append({"id": str(workspace.id), "name": workspace.name,
            "multiplier": display_rate(workspace.billing_multiplier_percent), "version": workspace.billing_version,
            "old_policies": old, "new_policies": new, "missing": sorted(set(missing)),
            "single_multiplier_preserves_prices": not changed and len(ratios) <= 1,
            "samples": comparisons})
        fingerprint.setdefault("workspaces", []).append({"id": str(workspace.id),
            "multiplier": workspace.billing_multiplier_percent, "version": workspace.billing_version,
            "bindings": [{"id": str(b.id), "provider_id": str(b.site_provider_id), "active": b.active} for b in bindings]})
    ready = not any(w["missing"] for w in workspaces)
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"fingerprint": digest, "ready": ready, "shared_pricing_enabled": settings.shared_pricing_enabled,
            "billing_enabled": settings.enabled, "paused": settings.pricing_paused, "workspaces": workspaces}


@router.get("/site/admin/pricing-migration")
def preview(db=Depends(get_db), actor=Depends(site_admin)):
    result = migration_preview(db)
    db.commit()
    return result


def assert_drained(db):
    if (db.scalar(select(Conversation.id).where(Conversation.active_request_id.is_not(None)).limit(1))
        or db.scalar(select(ModelUsageRequest.request_id).where(ModelUsageRequest.status == "running").limit(1))
        or db.scalar(select(IngestionJob.id).where(IngestionJob.state == JobState.RUNNING).limit(1))
        or db.scalar(select(BillingSnapshot.id).where(BillingSnapshot.event_key == "model",
                                                       BillingSnapshot.status == "running").limit(1))):
        raise ValueError("Drain running model and ingestion work before activation.")


def migration_command(db, operation, fingerprint=None):
    settings = settings_locked(db)
    if operation in ("pause", "resume"):
        settings.pricing_paused = operation == "pause"
        record(db, None, f"billing.pricing_{operation}", "pricing", 1, details={"source": "server_console"})
        db.flush()
        return {"paused": settings.pricing_paused, "shared_pricing_enabled": settings.shared_pricing_enabled}
    if operation == "preview":
        return migration_preview(db)
    if operation != "activate":
        raise ValueError("Unknown pricing operation")
    if settings.shared_pricing_enabled:
        return {"shared_pricing_enabled": True, "changed": False}
    if not settings.pricing_paused:
        raise ValueError("Pause new paid work and drain API/Worker before activation.")
    assert_drained(db)
    report = migration_preview(db)
    if fingerprint != report["fingerprint"]:
        raise ValueError("Pricing changed; review a fresh preview before activation.")
    if not report["ready"]:
        raise ValueError("Configure missing shared prices before activation.")
    settings.shared_pricing_enabled = True
    record(db, None, "billing.shared_pricing_activated", "pricing", 1,
        details={"source": "server_console", "fingerprint": fingerprint})
    db.flush()
    return {"shared_pricing_enabled": True, "changed": True, "paused": True}
