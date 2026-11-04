"""Administrator-managed payment adapter configuration."""
import re
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives.asymmetric import rsa

from . import payments
from .db import get_db
from .models import SitePaymentConfig
from .site_audit import record
from .site_auth import site_admin, protect

router = APIRouter(prefix="/site/admin/payments", tags=["site-admin-payments"])

from .recharge_settings import register_admin_routes
register_admin_routes(router)


class PaymentSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    version: int = Field(ge=0)
    provider_key: str = Field(default=payments.PROVIDER_KEY, pattern=r"^(zhifux|ezfp)$")
    enabled: bool
    api_base_url: str = Field(max_length=500)
    merchant_num: str = Field(max_length=100)
    pay_type: str = Field(max_length=100)
    payment_methods: list[payments.PaymentMethod] = Field(default_factory=list, max_length=20)
    custom_credits_per_yuan: int = Field(default=100, ge=1, le=1_000_000)
    secret: SecretStr | None = Field(default=None, max_length=500)
    private_key: SecretStr | None = Field(default=None, max_length=12000)
    public_key: SecretStr | None = Field(default=None, max_length=12000)
    clear_secret: bool = False
    custom_recharge_enabled: bool = False
    packages: list[payments.RechargePackage] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_configuration(self):
        if "payment_methods" not in self.model_fields_set and not self.payment_methods and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.pay_type or ""):
            object.__setattr__(self, "payment_methods", [payments.PaymentMethod(code=self.pay_type, label=self.pay_type)])
        if self.clear_secret and self.secret and self.secret.get_secret_value():
            raise ValueError("请勿同时输入新密钥和选择清除密钥")
        ids = [item.id for item in self.packages]
        if len(ids) != len(set(ids)):
            raise ValueError("充值档位 ID 不能重复")
        if self.enabled:
            if self.provider_key == payments.EZFP_PROVIDER_KEY:
                if self.api_base_url != payments.EZFP_ENDPOINT:
                    raise ValueError("易支付接口地址必须使用官方接口地址")
                if bool(self.private_key and self.private_key.get_secret_value()) != bool(self.public_key and self.public_key.get_secret_value()):
                    raise ValueError("商户私钥与平台公钥需要同时配置")
            elif not payments.https_url(self.api_base_url) or "?" in self.api_base_url:
                raise ValueError("支付网关必须是有效的 HTTPS 地址，且不能包含查询参数")
            if not re.fullmatch(r"[a-zA-Z0-9]{1,100}", self.merchant_num):
                raise ValueError("商户号格式不正确")
            if self.clear_secret and not (self.secret and self.secret.get_secret_value()):
                if self.provider_key != payments.EZFP_PROVIDER_KEY or not (self.private_key and self.private_key.get_secret_value() and self.public_key and self.public_key.get_secret_value()):
                    raise ValueError("启用支付时不能清除唯一的商户密钥")
        codes = [item.code for item in self.payment_methods]
        if len(codes) != len(set(codes)):
            raise ValueError("付款方式编码不能重复")
        if len({item.label for item in self.payment_methods}) != len(self.payment_methods):
            raise ValueError("付款方式名称不能重复")
        if self.payment_methods and self.pay_type not in codes:
            object.__setattr__(self, "pay_type", self.payment_methods[0].code)
        if self.provider_key == payments.EZFP_PROVIDER_KEY:
            try:
                private_raw = self.private_key.get_secret_value().encode() if self.private_key else b""
                public_raw = self.public_key.get_secret_value().encode() if self.public_key else b""
                if private_raw or public_raw:
                    private = payments._load_ezfp_key(private_raw.decode(), private=True)
                    public = payments._load_ezfp_key(public_raw.decode(), private=False)
                    if not isinstance(private, rsa.RSAPrivateKey) or not isinstance(public, rsa.RSAPublicKey):
                        raise ValueError("RSA keys required")
                    if private.key_size < 2048 or public.key_size < 2048:
                        raise ValueError("RSA keys must be at least 2048 bits")
                    object.__setattr__(self, "private_key", SecretStr(payments._ezfp_key_pem(private_raw.decode(), private=True)))
                    object.__setattr__(self, "public_key", SecretStr(payments._ezfp_key_pem(public_raw.decode(), private=False)))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("请填写有效的商户 RSA 私钥与平台 RSA 公钥（至少 2048 位）")
        return self


def current_row(db):
    return db.scalar(select(SitePaymentConfig).where(SitePaymentConfig.provider_key == payments.PROVIDER_KEY)
                     .order_by(SitePaymentConfig.version.desc()).limit(1))


def current_provider_row(db, provider_key):
    return db.scalar(select(SitePaymentConfig).where(SitePaymentConfig.provider_key == provider_key)
                     .order_by(SitePaymentConfig.version.desc()).limit(1))


@router.get("")
def read_settings(db: Session = Depends(get_db), actor=Depends(site_admin)):
    return payments.payment_admin_view(db)


@router.get("/{provider_key}")
def read_provider_settings(provider_key: str, db: Session = Depends(get_db), actor=Depends(site_admin)):
    if provider_key not in payments.PAYMENT_PROVIDERS:
        raise HTTPException(404, "支付渠道不存在")
    return payments.payment_admin_view(db, provider_key)


@router.put("")
def save_settings(payload: PaymentSettingsInput, request: Request,
                  db: Session = Depends(get_db), actor=Depends(site_admin)):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020049)"))
    provider_key = payload.provider_key
    current = current_provider_row(db, provider_key)
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
            "payment_methods": current.payment_methods,
            "custom_credits_per_yuan": current.custom_credits_per_yuan,
            "packages": current.packages,
            "custom_recharge_enabled": current.custom_recharge_enabled,
        }
        had_secret = bool(current.encrypted_secret)
    elif provider_key == payments.PROVIDER_KEY:
        try:
            legacy_packages = TypeAdapter(list[payments.RechargePackage]).validate_json(legacy.recharge_packages_json)
        except ValidationError:
            legacy_packages = []
        previous = {
            "enabled": legacy.zhifux_enabled,
            "api_base_url": legacy.zhifux_api_base_url,
            "merchant_num": legacy.zhifux_merchant_num,
            "pay_type": legacy.zhifux_pay_type,
            "payment_methods": [item.model_dump() for item in payments.configured_payment_methods(legacy)],
            "custom_credits_per_yuan": legacy.zhifux_custom_credits_per_yuan,
            "packages": [item.model_dump() for item in legacy_packages],
            "custom_recharge_enabled": legacy.zhifux_custom_recharge_enabled,
        }
        had_secret = bool(legacy.zhifux_secret and legacy.zhifux_secret.get_secret_value())
    else:
        previous = {"enabled": legacy.ezfp_enabled, "api_base_url": legacy.ezfp_api_base_url,
            "merchant_num": legacy.ezfp_pid, "pay_type": legacy.ezfp_pay_type, "packages": [],
            "payment_methods": [item.model_dump() for item in payments.configured_payment_methods(legacy, payments.EZFP_PROVIDER_KEY)],
            "custom_credits_per_yuan": legacy.ezfp_custom_credits_per_yuan,
            "custom_recharge_enabled": legacy.ezfp_custom_recharge_enabled}
        try:
            previous["packages"] = TypeAdapter(list[payments.RechargePackage]).validate_json(legacy.ezfp_recharge_packages_json)
            previous["packages"] = [item.model_dump() for item in previous["packages"]]
        except ValidationError:
            pass
        had_secret = bool(legacy.ezfp_private_key and legacy.ezfp_public_key)
    updated = {
        "enabled": payload.enabled,
        "api_base_url": payload.api_base_url,
        "merchant_num": payload.merchant_num,
        "pay_type": payload.pay_type,
        "payment_methods": [item.model_dump() for item in payload.payment_methods],
        "custom_credits_per_yuan": payload.custom_credits_per_yuan,
        "packages": [item.model_dump() for item in payload.packages],
        "custom_recharge_enabled": payload.custom_recharge_enabled,
    }
    changed_fields = [field for field, value in updated.items() if previous[field] != value]

    old_secret = current.encrypted_secret if current else None
    if current is None and provider_key == payments.PROVIDER_KEY and legacy.zhifux_secret:
        old_secret = protect(legacy.zhifux_secret.get_secret_value())
    elif current is None and provider_key == payments.EZFP_PROVIDER_KEY and had_secret:
        old_secret = protect(json.dumps({"private_key": legacy.ezfp_private_key.get_secret_value(),
            "public_key": legacy.ezfp_public_key.get_secret_value()}))
    if payload.clear_secret:
        encrypted_secret = None
    elif provider_key == payments.PROVIDER_KEY and payload.secret and payload.secret.get_secret_value():
        encrypted_secret = protect(payload.secret.get_secret_value())
    elif provider_key == payments.EZFP_PROVIDER_KEY and payload.private_key and payload.private_key.get_secret_value():
        encrypted_secret = protect(json.dumps({"private_key": payload.private_key.get_secret_value(),
            "public_key": payload.public_key.get_secret_value()}))
    else:
        encrypted_secret = old_secret

    secret_present = bool(encrypted_secret)
    if not payload.clear_secret and current is None and not (payload.secret and payload.secret.get_secret_value()) and not (payload.private_key and payload.private_key.get_secret_value()):
        secret_present = had_secret
    if payload.enabled and not secret_present:
        raise HTTPException(422, "启用支付前请配置商户密钥")
    secret_changed = (
        (payload.clear_secret and had_secret)
        or bool(payload.secret and payload.secret.get_secret_value())
        or bool(payload.private_key and payload.private_key.get_secret_value())
        or (current is None and had_secret and not payload.clear_secret)
    )
    if secret_changed:
        changed_fields.append("secret")

    version = (db.scalar(select(SitePaymentConfig.version).order_by(SitePaymentConfig.version.desc()).limit(1)) or 0) + 1
    row = SitePaymentConfig(
        version=version, provider_key=provider_key, enabled=payload.enabled,
        api_base_url=payload.api_base_url, merchant_num=payload.merchant_num,
        pay_type=payload.pay_type, encrypted_secret=encrypted_secret,
        payment_methods=[item.model_dump() for item in payload.payment_methods],
        custom_credits_per_yuan=payload.custom_credits_per_yuan,
        packages=[item.model_dump() for item in payload.packages],
        custom_recharge_enabled=payload.custom_recharge_enabled, created_by=actor.id,
    )
    db.add(row)
    record(db, actor, "payment.settings_updated", "payment_config", version,
           request=request, details={"fields": changed_fields, "version": version, "provider_key": provider_key})
    db.commit()
    return payments.payment_admin_view(db, provider_key)


@router.put("/{provider_key}")
def save_provider_settings(provider_key: str, payload: PaymentSettingsInput, request: Request,
                           db: Session = Depends(get_db), actor=Depends(site_admin)):
    if provider_key not in payments.PAYMENT_PROVIDERS or payload.provider_key != provider_key:
        raise HTTPException(404, "支付渠道不存在")
    return save_settings(payload, request, db, actor)
