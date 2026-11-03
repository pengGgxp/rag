"""Administrator-managed payment adapter configuration."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import payments
from .db import get_db
from .models import SitePaymentConfig
from .site_audit import record
from .site_auth import site_admin, protect

router = APIRouter(prefix="/site/admin/payments", tags=["site-admin-payments"])


class PaymentSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    version: int = Field(ge=0)
    enabled: bool
    api_base_url: str = Field(max_length=500)
    merchant_num: str = Field(max_length=100)
    pay_type: str = Field(max_length=100)
    secret: SecretStr | None = Field(default=None, max_length=500)
    clear_secret: bool = False
    packages: list[payments.RechargePackage] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.clear_secret and self.secret and self.secret.get_secret_value():
            raise ValueError("请勿同时输入新密钥和选择清除密钥")
        ids = [item.id for item in self.packages]
        if len(ids) != len(set(ids)):
            raise ValueError("充值档位 ID 不能重复")
        if self.enabled:
            if not self.packages:
                raise ValueError("启用充值前至少需要配置一个充值档位")
            if not payments.https_url(self.api_base_url) or "?" in self.api_base_url:
                raise ValueError("支付网关必须是有效的 HTTPS 地址，且不能包含查询参数")
            if not re.fullmatch(r"[a-zA-Z0-9]{1,100}", self.merchant_num):
                raise ValueError("商户号格式不正确")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.pay_type):
                raise ValueError("支付产品编码格式不正确")
            if self.clear_secret and not (self.secret and self.secret.get_secret_value()):
                raise ValueError("启用支付时不能清除唯一的商户密钥")
        return self


def current_row(db):
    return db.scalar(select(SitePaymentConfig).order_by(SitePaymentConfig.version.desc()).limit(1))


@router.get("")
def read_settings(db: Session = Depends(get_db), actor=Depends(site_admin)):
    return payments.payment_admin_view(db)


@router.put("")
def save_settings(payload: PaymentSettingsInput, request: Request,
                  db: Session = Depends(get_db), actor=Depends(site_admin)):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020049)"))
    current = current_row(db)
    current_version = current.version if current else 0
    if payload.version != current_version:
        raise HTTPException(409, "支付设置已更新，请重新加载后再保存")

    legacy = payments.get_settings()
    if current:
        previous = {
            "enabled": current.enabled,
            "api_base_url": current.api_base_url,
            "merchant_num": current.merchant_num,
            "pay_type": current.pay_type,
            "packages": current.packages,
        }
        had_secret = bool(current.encrypted_secret)
    else:
        try:
            legacy_packages = TypeAdapter(list[payments.RechargePackage]).validate_json(legacy.recharge_packages_json)
        except ValidationError:
            legacy_packages = []
        previous = {
            "enabled": legacy.zhifux_enabled,
            "api_base_url": legacy.zhifux_api_base_url,
            "merchant_num": legacy.zhifux_merchant_num,
            "pay_type": legacy.zhifux_pay_type,
            "packages": [item.model_dump() for item in legacy_packages],
        }
        had_secret = bool(legacy.zhifux_secret and legacy.zhifux_secret.get_secret_value())
    updated = {
        "enabled": payload.enabled,
        "api_base_url": payload.api_base_url,
        "merchant_num": payload.merchant_num,
        "pay_type": payload.pay_type,
        "packages": [item.model_dump() for item in payload.packages],
    }
    changed_fields = [field for field, value in updated.items() if previous[field] != value]

    old_secret = current.encrypted_secret if current else None
    if current is None and legacy.zhifux_secret:
        old_secret = protect(legacy.zhifux_secret.get_secret_value())
    if payload.clear_secret:
        encrypted_secret = None
    elif payload.secret and payload.secret.get_secret_value():
        encrypted_secret = protect(payload.secret.get_secret_value())
    else:
        encrypted_secret = old_secret

    secret_present = bool(encrypted_secret)
    if not payload.clear_secret and current is None and not (payload.secret and payload.secret.get_secret_value()):
        secret_present = bool(legacy.zhifux_secret and legacy.zhifux_secret.get_secret_value())
    if payload.enabled and not secret_present:
        raise HTTPException(422, "启用支付前请配置商户密钥")
    secret_changed = (
        (payload.clear_secret and had_secret)
        or bool(payload.secret and payload.secret.get_secret_value())
        or (current is None and had_secret and not payload.clear_secret)
    )
    if secret_changed:
        changed_fields.append("secret")

    version = current_version + 1
    row = SitePaymentConfig(
        version=version, provider_key=payments.PROVIDER_KEY, enabled=payload.enabled,
        api_base_url=payload.api_base_url, merchant_num=payload.merchant_num,
        pay_type=payload.pay_type, encrypted_secret=encrypted_secret,
        packages=[item.model_dump() for item in payload.packages], created_by=actor.id,
    )
    db.add(row)
    record(db, actor, "payment.settings_updated", "payment_config", version,
           request=request, details={"fields": changed_fields, "version": version})
    db.commit()
    return payments.payment_admin_view(db)
