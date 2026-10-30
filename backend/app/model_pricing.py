"""Explicit model prices and one-time defaults; no implicit cache discounts."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .model_billing import CAPABILITIES
from .models import (ContextOperation, Conversation, LLMProviderConfig, ModelBillingSettings,
                     ModelConnection, ModelUsageRequest, SiteModelProvider, UsagePolicy, User, Workspace)
from .site_auth import site_admin
from .site_audit import record
from .credit_amounts import CreditRate

router = APIRouter(prefix="/site/admin/pricing-template", tags=["site-admin"])
DOCUMENT_CAPABILITIES = ("ocr_pages", "embedding_tokens")
TEMPLATE_CAPABILITIES = (*CAPABILITIES, *DOCUMENT_CAPABILITIES)


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: str = Field(pattern=r"^llm_(input|cached_input|output)_tokens$")
    unit: str = Field(pattern=r"^tokens$")
    unit_size: int = Field(ge=1, le=2_147_483_647)
    credits_per_unit: CreditRate
    enabled: bool = True


class TemplatePrice(ModelPrice):
    capability: str = Field(pattern=r"^(llm_(input|cached_input|output)_tokens|ocr_pages|embedding_tokens)$")
    unit: str = Field(pattern=r"^(tokens|pages)$")

    @model_validator(mode="after")
    def valid_unit(self):
        if self.unit != ("pages" if self.capability == "ocr_pages" else "tokens"):
            raise ValueError("Unit does not match capability")
        return self


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    policies: list[TemplatePrice] = Field(max_length=5)


def serialize_policy(row):
    return {"capability": row.capability, "unit": row.unit, "unit_size": row.unit_size,
        "credits_per_unit": row.credits_per_unit, "enabled": row.enabled, "version": row.version,
        "configured": True, "template_version": row.template_version}


def prices_read(rows, *, all_model=True):
    result = [serialize_policy(row) for row in rows]
    for key in CAPABILITIES if all_model else ():
        if not any(item["capability"] == key for item in result):
            result.append({"capability": key, "unit": "tokens", "unit_size": 1000,
                "credits_per_unit": None, "enabled": True, "version": 0,
                "configured": False, "template_version": None})
    return sorted(result, key=lambda item: item["capability"])


def complete(template):
    if set(template) != set(CAPABILITIES):
        return False
    try:
        return all(ModelPrice.model_validate(template[key]).enabled and template[key]["capability"] == key
            for key in CAPABILITIES)
    except (ValueError, KeyError, TypeError):
        return False


def settings_locked(db):
    row = db.get(ModelBillingSettings, 1, with_for_update=True, populate_existing=True)
    if row is None:
        row = ModelBillingSettings(id=1, enabled=False, template={}, version=0)
        db.add(row)
        db.flush()
    return row


def template_read(row):
    policies = []
    for key in TEMPLATE_CAPABILITIES:
        policies.append({**row.template.get(key, {"capability": key, "unit": "pages" if key == "ocr_pages" else "tokens",
            "unit_size": 1 if key == "ocr_pages" else 1000, "credits_per_unit": None, "enabled": True}),
            "configured": key in row.template, "version": row.version, "template_version": None})
    return {"version": row.version, "configured": set(row.template) == set(TEMPLATE_CAPABILITIES),
        "billing_enabled": row.enabled, "shared_pricing_enabled": bool(row.shared_pricing_enabled), "policies": policies}


@router.get("")
def read_template(db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    row = db.get(ModelBillingSettings, 1)
    return template_read(row or ModelBillingSettings(enabled=False, template={}, version=0))


def template_writer(request: Request, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    try:
        yield actor
    except Exception:
        db.rollback()
        record(db, actor, "billing.template_update_failed", "pricing_template", 1,
            request=request, result="failure", details={"fields": list(TEMPLATE_CAPABILITIES)})
        db.commit()
        raise


@router.put("")
def update_template(payload: TemplateUpdate, db: Session = Depends(get_db), actor: User = Depends(template_writer)):
    template = {item.capability: item.model_dump() for item in payload.policies}
    if len(template) != len(payload.policies):
        raise HTTPException(422, "计费能力不能重复")
    row = settings_locked(db)
    if payload.version != row.version:
        raise HTTPException(409, "价格模板已更新，请重新加载后保存")
    row.template, row.version = template, row.version + 1
    record(db, actor, "billing.template_updated", "pricing_template", 1, details={"fields": list(TEMPLATE_CAPABILITIES)})
    db.commit()
    return template_read(row)


def switch_billing(db, operation):
    """Console gate. The operator must drain traffic before changing modes."""
    from .model_billing import price_snapshot
    row = settings_locked(db)
    if operation == "status":
        return template_read(row)
    if db.scalar(select(Conversation.id).where(Conversation.active_request_id.is_not(None)).limit(1)) or db.scalar(
        select(ModelUsageRequest.request_id).where(ModelUsageRequest.status == "running").limit(1)) or db.scalar(
        select(ContextOperation.request_id).where(ContextOperation.status == "running").limit(1)):
        raise ValueError("Drain active model requests before switching billing.")
    if operation == "enable":
        if row.shared_pricing_enabled:
            # Request-level selection also exposes models without a workspace binding.
            providers = db.scalars(select(SiteModelProvider).join(ModelConnection).where(
                SiteModelProvider.enabled.is_(True), ModelConnection.enabled.is_(True)))
            for provider in providers:
                if not complete(provider.prices):
                    raise ValueError(f"Configure all three model prices for {provider.name} ({provider.id}).")
        else:
            if not complete({k: v for k, v in row.template.items() if k in CAPABILITIES}):
                raise ValueError("Configure the complete model pricing template first.")
            for workspace in db.scalars(select(Workspace)):
                active = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace.id,
                    LLMProviderConfig.active.is_(True), LLMProviderConfig.enabled.is_(True)))
                price_snapshot(db, workspace.id, active.id if active else None)
    elif operation == "disable":
        if db.scalar(select(ModelUsageRequest.request_id).where(ModelUsageRequest.status == "pending").limit(1)):
            raise ValueError("Settle pending charges before disabling model billing.")
    else:
        raise ValueError("Unknown billing operation")
    row.enabled = operation == "enable"
    record(db, None, f"billing.{operation}", "pricing_template", 1, details={"source": "server_console"})
    db.flush()
    return template_read(row)
