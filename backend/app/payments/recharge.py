"""Site-wide recharge prices and explicit cashier method routing."""
import json
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select, text

from app.payments import orders as payments
from app.infra.db import get_db
from app.models import SiteRechargeConfig
from app.operations.site_audit import record
from app.identity.site_auth import site_admin


class CashierMethod(payments.PaymentMethod):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,120}$")
    provider_key: Literal["zhifux", "ezfp"]
    enabled: bool = True


class RechargeSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    packages: list[payments.RechargePackage] = Field(max_length=20)
    custom_recharge_enabled: bool
    custom_credits_per_yuan: int = Field(ge=1, le=1_000_000)
    payment_methods: list[CashierMethod] = Field(max_length=40)

    @model_validator(mode="after")
    def unique_items(self):
        if len({p.id for p in self.packages}) != len(self.packages):
            raise ValueError("充值档位 ID 不能重复")
        if len({m.id for m in self.payment_methods}) != len(self.payment_methods):
            raise ValueError("付款方式 ID 不能重复")
        if len({(m.provider_key, m.code) for m in self.payment_methods}) != len(self.payment_methods):
            raise ValueError("同一渠道的网关编码不能重复")
        return self


def current_config(db):
    row = db.scalar(select(SiteRechargeConfig).order_by(SiteRechargeConfig.version.desc()).limit(1))
    if row:
        return {"version": row.version, "source": "database", "packages": row.packages,
                "custom_recharge_enabled": row.custom_recharge_enabled,
                "custom_credits_per_yuan": row.custom_credits_per_yuan, "payment_methods": row.payment_methods}
    # Fresh installations may still have environment-only merchant settings.
    candidates = []
    for key in (payments.EZFP_PROVIDER_KEY, payments.PROVIDER_KEY):
        settings, _ = payments.effective_settings(db, provider_key=key)
        enabled = settings.ezfp_enabled if key == payments.EZFP_PROVIDER_KEY else settings.zhifux_enabled
        raw = settings.ezfp_recharge_packages_json if key == payments.EZFP_PROVIDER_KEY else settings.recharge_packages_json
        try:
            packages = [p.model_dump() for p in TypeAdapter(list[payments.RechargePackage]).validate_json(raw)]
        except ValidationError:
            if enabled:
                raise HTTPException(503, "充值报价配置无效，请联系管理员")
            packages = []
        candidates.append({"enabled": enabled, "packages": packages,
            "custom_recharge_enabled": settings.ezfp_custom_recharge_enabled if key == "ezfp" else settings.zhifux_custom_recharge_enabled,
            "custom_credits_per_yuan": settings.ezfp_custom_credits_per_yuan if key == "ezfp" else settings.zhifux_custom_credits_per_yuan,
            "payment_methods": [{"id": f"{key}_{m.code}", "label": m.label, "code": m.code,
                                 "provider_key": key, "enabled": True}
                                for m in payments.configured_payment_methods(settings, key)] if enabled else []})
    active = [c for c in candidates if c["enabled"]]
    source = next(iter(active), candidates[-1])
    def prices(c):
        return (json.dumps(sorted(c["packages"], key=lambda p: p["id"]), sort_keys=True),
                c["custom_recharge_enabled"], c["custom_credits_per_yuan"])
    if any(prices(c) != prices(source) for c in active):
        raise HTTPException(409, "充值报价存在差异，请管理员确认统一充值设置")
    return {"version": 0, "source": "environment", "packages": source["packages"],
            "custom_recharge_enabled": source["custom_recharge_enabled"],
            "custom_credits_per_yuan": source["custom_credits_per_yuan"],
            "payment_methods": [m for c in active for m in c["payment_methods"]]}


def available_methods(db, config):
    providers = {}
    for key in payments.PAYMENT_PROVIDERS:
        try:
            settings, version = payments.effective_settings(db, provider_key=key)
            payments.validate_provider(settings, key)
            providers[key] = (settings, version)
        except HTTPException:
            continue
    return [(method, *providers[method["provider_key"]]) for method in config["payment_methods"]
            if method["enabled"] and method["provider_key"] in providers]


def config_options(config):
    # Reuse the existing integer arithmetic and quotation rules with unified prices.
    settings = payments.Settings(_env_file=None).model_copy(update={
        "zhifux_custom_recharge_enabled": config["custom_recharge_enabled"],
        "zhifux_custom_credits_per_yuan": config["custom_credits_per_yuan"]})
    packages = TypeAdapter(list[payments.RechargePackage]).validate_python(config["packages"])
    return payments.recharge_options(settings, packages=packages)


def resolve_selection(config, payload):
    packages, custom = config_options(config)
    return payments.resolve_selection(packages, custom, payload)


def catalog(db):
    try:
        config = current_config(db)
        if not available_methods(db, config):
            return {"enabled": False, "packages": []}
        packages, custom = config_options(config)
    except HTTPException:
        return {"enabled": False, "packages": []}
    return {"enabled": bool(packages or custom),
            "packages": [{**p.model_dump(), "quote": payments.package_quote(p)} for p in packages],
            **({"custom": custom} if custom else {})}


def register_admin_routes(router):
    @router.get("/recharge")
    def read(db=Depends(get_db), actor=Depends(site_admin)):
        return current_config(db)

    @router.put("/recharge")
    def save(payload: RechargeSettingsInput, request: Request, db=Depends(get_db), actor=Depends(site_admin)):
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(3020049)"))
        current = current_config(db)
        if payload.version != current["version"]:
            raise HTTPException(409, "充值设置已更新，请重新加载后再保存")
        data = payload.model_dump(exclude={"version"})
        row = SiteRechargeConfig(version=payload.version + 1, **data, created_by=actor.id)
        db.add(row)
        record(db, actor, "payment.recharge_settings_updated", "recharge_config", row.version,
               request=request, details={"fields": [k for k in data if data[k] != current[k]], "version": row.version})
        db.commit()
        return current_config(db)
