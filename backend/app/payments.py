"""Server-owned recharge orders and payment provider adapters."""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4
import base64

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import auth
from .audit_log import record_audit
from .auth import Access, aware, get_access, now
from .config import Settings, get_settings
from .credits import recharge
from .models import RechargeOrder, SitePaymentConfig, Workspace

CALLBACK_PATH = "/payments/zhifux/notify"
EZFP_CALLBACK_PATH = "/payments/ezfp/notify"
EZFP_ENDPOINT = "https://www.ezfp.cn/api/pay/create"
PAY_DURATION_MINUTES = 15
PROVIDER_KEY = "zhifux"
EZFP_PROVIDER_KEY = "ezfp"
PAYMENT_PROVIDERS = {
    PROVIDER_KEY: {"name": "支付 FM", "website_url": "https://wmp.zhifux.com/"},
    EZFP_PROVIDER_KEY: {"name": "易支付", "website_url": "https://www.ezfp.cn/"},
}


def effective_settings(db, *, base=None, version=None, provider_key=PROVIDER_KEY):
    """Resolve a payment adapter configuration without exposing its secret."""
    settings = base or get_settings()
    row = db.get(SitePaymentConfig, version) if version is not None else db.scalar(
        select(SitePaymentConfig).where(SitePaymentConfig.provider_key == provider_key)
        .order_by(SitePaymentConfig.version.desc()).limit(1))
    if row is not None and row.provider_key != provider_key:
        raise HTTPException(503, "支付渠道配置版本不匹配")
    if version is not None and row is None:
        raise HTTPException(503, "支付渠道配置版本不存在")
    if row is None:
        return settings, None
    from .site_auth import reveal
    secret = reveal(row.encrypted_secret) if row.encrypted_secret else ""
    if provider_key == EZFP_PROVIDER_KEY:
        credentials = {}
        try:
            credentials = json.loads(secret) if secret else {}
        except (TypeError, ValueError):
            raise HTTPException(503, "易支付密钥配置无法读取")
        return settings.model_copy(update={
            "ezfp_enabled": row.enabled,
            "ezfp_api_base_url": row.api_base_url,
            "ezfp_pid": row.merchant_num,
            "ezfp_pay_type": row.pay_type,
            "ezfp_private_key": SecretStr(credentials["private_key"]) if credentials.get("private_key") else None,
            "ezfp_public_key": SecretStr(credentials["public_key"]) if credentials.get("public_key") else None,
            "ezfp_recharge_packages_json": json.dumps(row.packages, ensure_ascii=False),
        }), row.version
    return settings.model_copy(update={
        "zhifux_enabled": row.enabled,
        "zhifux_api_base_url": row.api_base_url,
        "zhifux_merchant_num": row.merchant_num,
        "zhifux_pay_type": row.pay_type,
        "zhifux_secret": SecretStr(secret) if secret else None,
        "recharge_packages_json": json.dumps(row.packages, ensure_ascii=False),
    }), row.version


def payment_admin_view(db, provider_key=PROVIDER_KEY):
    settings, version = effective_settings(db, provider_key=provider_key)
    from .site_auth import cipher, reveal
    try:
        cipher()
        encryption_ready = True
    except HTTPException:
        encryption_ready = False
    row = db.get(SitePaymentConfig, version) if version is not None else None
    if provider_key == EZFP_PROVIDER_KEY:
        enabled, api_base_url = settings.ezfp_enabled, settings.ezfp_api_base_url
        merchant_num, pay_type = settings.ezfp_pid, settings.ezfp_pay_type
        packages_json = settings.ezfp_recharge_packages_json
        if row:
            credentials = json.loads(reveal(row.encrypted_secret)) if row.encrypted_secret else {}
            has_secret = bool(credentials.get("private_key") and credentials.get("public_key"))
        else:
            has_secret = bool(settings.ezfp_private_key and settings.ezfp_public_key)
        callback_path = EZFP_CALLBACK_PATH
    else:
        enabled, api_base_url = settings.zhifux_enabled, settings.zhifux_api_base_url
        merchant_num, pay_type = settings.zhifux_merchant_num, settings.zhifux_pay_type
        packages_json = settings.recharge_packages_json
        has_secret = bool(row.encrypted_secret) if row else bool(settings.zhifux_secret and settings.zhifux_secret.get_secret_value())
        callback_path = CALLBACK_PATH
    packages = []
    try:
        packages = [item.model_dump() for item in TypeAdapter(list[RechargePackage]).validate_json(packages_json)]
    except ValidationError:
        pass
    return {
        "version": version or 0,
        "source": "database" if row else "environment",
        "enabled": enabled,
        "provider_key": provider_key,
        "provider_name": PAYMENT_PROVIDERS[provider_key]["name"],
        "website_url": PAYMENT_PROVIDERS[provider_key]["website_url"],
        "api_base_url": api_base_url,
        "merchant_num": merchant_num,
        "pay_type": pay_type,
        "has_secret": has_secret,
        "packages": packages,
        "callback_url": settings.api_public_url.rstrip("/") + callback_path,
        "return_url": settings.frontend_url.rstrip("/") + "/?account=1&billing=1",
        "encryption_ready": encryption_ready,
    }


class RechargePackage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    amount_fen: int = Field(ge=1, le=1_000_000)
    credits: int = Field(ge=1, le=1_000_000)


class CreateOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    idempotency_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    quote: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_key: str = Field(default=PROVIDER_KEY, pattern=r"^(zhifux|ezfp)$")
    custom_amount_fen: int | None = Field(default=None, strict=True, ge=100, le=1_000_000)


def package_quote(package: RechargePackage) -> str:
    return hashlib.sha256(package.model_dump_json().encode("utf-8")).hexdigest()


CUSTOM_PACKAGE_ID = "custom_amount"


def recharge_options(settings: Settings, provider_key=PROVIDER_KEY):
    packages = configured_packages(settings, provider_key)
    if not packages:
        return [], None
    # Added amounts never introduce a better exchange rate than existing offers.
    reference = min(packages, key=lambda item: Fraction(item.credits, item.amount_fen))
    rate = Fraction(reference.credits, reference.amount_fen)
    minimum = max(100, (rate.denominator + rate.numerator - 1) // rate.numerator)
    maximum = min(1_000_000, 1_000_000 * rate.denominator // rate.numerator)
    identifiers = {item.id for item in packages}
    custom_id = CUSTOM_PACKAGE_ID
    while custom_id in identifiers:
        custom_id += "_"
    options = list(packages)
    for fen in (2000, 5000, 10000, 20000):
        credits = fen * rate.numerator // rate.denominator
        if minimum <= fen <= maximum and not any(item.amount_fen == fen for item in options):
            identifier = f"amount_{fen}"
            while identifier in identifiers:
                identifier += "_"
            options.append(RechargePackage(id=identifier, name=f"{fen // 100} 元充值",
                                           amount_fen=fen, credits=credits))
    options.sort(key=lambda item: item.amount_fen)
    custom = {"id": custom_id, "min_amount_fen": minimum, "max_amount_fen": maximum,
              "rate_credits": reference.credits, "rate_amount_fen": reference.amount_fen,
              "quote": package_quote(reference)} if minimum <= maximum else None
    return options, custom


def resolve_package(settings: Settings, payload: CreateOrder, provider_key=PROVIDER_KEY):
    packages, custom = recharge_options(settings, provider_key)
    if custom and payload.package_id == custom["id"]:
        if payload.custom_amount_fen is None:
            raise HTTPException(422, "请输入充值金额")
        fen = payload.custom_amount_fen
        if not custom["min_amount_fen"] <= fen <= custom["max_amount_fen"]:
            raise HTTPException(422, "充值金额超出范围，请重新输入")
        if payload.quote != custom["quote"]:
            raise HTTPException(409, "充值价格已更新，请刷新并确认到账积分后重试")
        return RechargePackage(id=custom["id"], name="自定义充值", amount_fen=fen,
                               credits=fen * custom["rate_credits"] // custom["rate_amount_fen"])
    if payload.custom_amount_fen is not None:
        raise HTTPException(422, "请选择自定义金额后再输入金额")
    package = next((item for item in packages if item.id == payload.package_id), None)
    if package is None:
        raise HTTPException(409, "该充值档位已不可用，请刷新后重试")
    if payload.quote != package_quote(package):
        raise HTTPException(409, "充值档位已更新，请刷新并确认价格后重试")
    return package


def https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value)
        )
    except ValueError:
        return False


def configured_packages(settings: Settings, provider_key=PROVIDER_KEY) -> list[RechargePackage]:
    if provider_key == EZFP_PROVIDER_KEY:
        enabled, api_url, merchant, pay_type = settings.ezfp_enabled, settings.ezfp_api_base_url, settings.ezfp_pid, settings.ezfp_pay_type
        raw_packages = settings.ezfp_recharge_packages_json
        secret_ready = bool(settings.ezfp_private_key and settings.ezfp_public_key)
    else:
        enabled, api_url, merchant, pay_type = settings.zhifux_enabled, settings.zhifux_api_base_url, settings.zhifux_merchant_num, settings.zhifux_pay_type
        raw_packages = settings.recharge_packages_json
        secret_ready = bool(settings.zhifux_secret and settings.zhifux_secret.get_secret_value())
    if not enabled:
        return []
    try:
        packages = TypeAdapter(list[RechargePackage]).validate_json(raw_packages)
        roots = [api_url, settings.api_public_url, settings.frontend_url]
        valid = (
            1 <= len(packages) <= 20
            and len({item.id for item in packages}) == len(packages)
            and all(https_url(root) and not urlsplit(root).query for root in roots)
            and (provider_key != EZFP_PROVIDER_KEY or api_url == EZFP_ENDPOINT)
            and len(settings.api_public_url.rstrip("/") + (EZFP_CALLBACK_PATH if provider_key == EZFP_PROVIDER_KEY else CALLBACK_PATH)) <= 200
            and len(settings.frontend_url.rstrip("/") + "/?account=1&billing=1") <= 200
            and bool(re.fullmatch(r"[a-zA-Z0-9]{1,100}", merchant))
            and bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", pay_type))
            and secret_ready
        )
        if valid:
            return packages
    except (ValidationError, ValueError):
        pass
    raise HTTPException(503, "充值暂不可用，请稍后再试")


def md5_sign(value: str) -> str:
    # Required by the provider protocol, not used for passwords or sessions.
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def yuan(amount_fen: int) -> str:
    return f"{amount_fen // 100}.{amount_fen % 100:02d}"


class GatewayError(Exception):
    def __init__(self, *, rejected: bool = False):
        self.rejected = rejected


@dataclass(frozen=True)
class Checkout:
    platform_order_no: str
    pay_url: str


class PaymentAdapter(Protocol):
    provider_key: str
    callback_path: str

    def create_checkout(self, settings: Settings, order: RechargeOrder, client_ip: str = "") -> Checkout: ...
    def settings_for_order(self, db: Session, order: RechargeOrder | None, fallback: Settings) -> Settings: ...
    def verify_notification(self, params: dict[str, str], settings: Settings) -> tuple[str, str, str]: ...
    def notification_order_no(self, params: dict[str, str]) -> str: ...


def start_checkout(settings: Settings, order: RechargeOrder) -> Checkout:
    amount = yuan(order.amount_fen)
    notify_url = settings.api_public_url.rstrip("/") + CALLBACK_PATH
    params = {
        "merchantNum": order.merchant_num,
        "orderNo": order.id.hex,
        "amount": amount,
        "notifyUrl": notify_url,
        "returnUrl": settings.frontend_url.rstrip("/") + "/?account=1&billing=1",
        "payType": order.pay_type,
        "returnType": "json",
        "apiMode": "post_form",
        "payDuration": str(PAY_DURATION_MINUTES),
        "subject": f"知库积分充值 - {order.package_name}"[:100],
        "sign": md5_sign(
            order.merchant_num + order.id.hex + amount + notify_url
            + settings.zhifux_secret.get_secret_value()
        ),
    }
    try:
        # No redirects/retries or exception logging: URLs contain the payment signature.
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            with client.stream(
                "POST", settings.zhifux_api_base_url.rstrip("/") + "/startOrder",
                params=params, headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 65_536:
                        raise GatewayError()
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise GatewayError()
                if response.status_code in (200, 400) and payload.get("success") is False:
                    raise GatewayError(rejected=True)
                data = payload.get("data")
                if response.status_code != 200 or payload.get("success") is not True or not isinstance(data, dict):
                    raise GatewayError()
                platform_id = data.get("id")
                pay_url = data.get("payUrl")
                if (
                    not isinstance(platform_id, (str, int)) or isinstance(platform_id, bool)
                    or not re.fullmatch(r"[a-zA-Z0-9]{1,100}", str(platform_id))
                    or not isinstance(pay_url, str) or len(pay_url) > 4096 or not https_url(pay_url)
                ):
                    raise GatewayError()
                return Checkout(str(platform_id), pay_url)
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        raise GatewayError() from exc


class PaymentFMAdapter:
    provider_key = PROVIDER_KEY
    callback_path = CALLBACK_PATH

    def create_checkout(self, settings: Settings, order: RechargeOrder, client_ip: str = "") -> Checkout:
        return start_checkout(settings, order)

    def settings_for_order(self, db: Session, order: RechargeOrder | None, fallback: Settings) -> Settings:
        if order is None or order.payment_config_version is None:
            return fallback
        settings, _ = effective_settings(db, base=fallback, version=order.payment_config_version,
                                         provider_key=self.provider_key)
        return settings

    def notification_order_no(self, params: dict[str, str]) -> str:
        return params.get("orderNo", "")

    def verify_notification(self, params: dict[str, str], settings: Settings) -> tuple[str, str, str]:
        secret = settings.zhifux_secret
        if not secret or not secret.get_secret_value():
            raise HTTPException(503, "fail")
        state = params.get("state", "")
        merchant = params.get("merchantNum", "")
        order_no = params.get("orderNo", "")
        amount = params.get("amount", "")
        platform_id = params.get("platformOrderNo", "")
        signature = params.get("sign", "")
        if (
            state != "1" or merchant != settings.zhifux_merchant_num
            or not re.fullmatch(r"[a-f0-9]{32}", order_no)
            or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", amount)
            or not re.fullmatch(r"[a-zA-Z0-9]{1,100}", platform_id)
            or not re.fullmatch(r"[a-fA-F0-9]{32}", signature)
            or not secrets.compare_digest(
                signature.lower(), md5_sign(state + merchant + order_no + amount + secret.get_secret_value()),
            )
        ):
            raise HTTPException(400, "fail")
        return merchant, amount, platform_id


def _signed_fields(params: dict[str, str]) -> str:
    fields = [(key, value) for key, value in params.items()
              if key not in ("sign", "sign_type") and value is not None and value != ""]
    fields.sort(key=lambda item: item[0].encode("ascii", errors="strict"))
    return "&".join(f"{key}={value}" for key, value in fields)


def _verify_rsa(params, settings):
    signature = params.get("sign", "")
    if params.get("sign_type", "RSA").upper() != "RSA" or not signature or len(signature) > 2048:
        return False
    try:
        public_key = serialization.load_pem_public_key(settings.ezfp_public_key.get_secret_value().encode())
        public_key.verify(base64.b64decode(signature, validate=True), _signed_fields(params).encode("utf-8"),
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except (ValueError, TypeError, InvalidSignature, UnicodeError, AttributeError):
        return False


class EzfpAdapter:
    provider_key = EZFP_PROVIDER_KEY
    callback_path = EZFP_CALLBACK_PATH
    endpoint = EZFP_ENDPOINT

    def create_checkout(self, settings: Settings, order: RechargeOrder, client_ip: str = "") -> Checkout:
        if not settings.ezfp_private_key:
            raise GatewayError(rejected=True)
        params = {"pid": settings.ezfp_pid, "method": "jump", "type": order.pay_type,
            "out_trade_no": order.id.hex, "notify_url": settings.api_public_url.rstrip("/") + self.callback_path,
            "return_url": settings.frontend_url.rstrip("/") + "/?account=1&billing=1",
            "name": f"知库积分充值 - {order.package_name}"[:100], "money": yuan(order.amount_fen),
            "clientip": client_ip, "timestamp": str(int(now().timestamp())), "sign_type": "RSA"}
        try:
            private_key = serialization.load_pem_private_key(settings.ezfp_private_key.get_secret_value().encode(), password=None)
            params["sign"] = base64.b64encode(private_key.sign(_signed_fields(params).encode("utf-8"),
                padding.PKCS1v15(), hashes.SHA256())).decode("ascii")
        except (ValueError, TypeError, AttributeError) as exc:
            raise GatewayError(rejected=True) from exc
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                with client.stream("POST", self.endpoint, data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise GatewayError()
                    result = json.loads(body)
                    if response.status_code != 200 or not isinstance(result, dict) or not _verify_rsa(result, settings):
                        raise GatewayError()
                    if str(result.get("code")) != "0":
                        raise GatewayError(rejected=True)
                    trade_no, pay_url = result.get("trade_no"), result.get("pay_info")
                    if (not isinstance(trade_no, (str, int)) or isinstance(trade_no, bool)
                        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(trade_no))
                        or result.get("pay_type") != "jump" or not isinstance(pay_url, str)
                        or len(pay_url) > 4096 or not https_url(pay_url)):
                        raise GatewayError()
                    return Checkout(str(trade_no), pay_url)
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            raise GatewayError() from exc

    def settings_for_order(self, db: Session, order: RechargeOrder | None, fallback: Settings) -> Settings:
        if order is None or order.payment_config_version is None:
            return fallback
        settings, _ = effective_settings(db, base=fallback, version=order.payment_config_version,
                                         provider_key=self.provider_key)
        return settings

    def notification_order_no(self, params: dict[str, str]) -> str:
        return params.get("out_trade_no", "")

    def verify_notification(self, params, settings):
        order_no = self.notification_order_no(params)
        merchant, amount, platform_id = params.get("pid", ""), params.get("money", ""), params.get("trade_no", "")
        if (params.get("trade_status") != "TRADE_SUCCESS" or merchant != settings.ezfp_pid
            or not re.fullmatch(r"[a-f0-9]{32}", order_no)
            or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", amount)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", platform_id)
            or not _verify_rsa(params, settings)):
            raise HTTPException(400, "fail")
        return merchant, amount, platform_id


PAYMENT_ADAPTERS: dict[str, PaymentAdapter] = {PROVIDER_KEY: PaymentFMAdapter(), EZFP_PROVIDER_KEY: EzfpAdapter()}


def payment_adapter(provider_key: str) -> PaymentAdapter:
    adapter = PAYMENT_ADAPTERS.get(provider_key)
    if adapter is None:
        raise HTTPException(503, "支付渠道暂不可用")
    return adapter


def order_read(order: RechargeOrder) -> dict:
    status = order.status
    if status in ("pending", "creating", "confirming") and order.expires_at and aware(order.expires_at) <= now():
        status = "expired"
    return {
        "id": str(order.id),
        "order_no": order.id.hex,
        "package_name": order.package_name,
        "amount_fen": order.amount_fen,
        "credits": order.credits,
        "status": status,
        "pay_url": order.pay_url if status == "pending" else None,
        "created_at": order.created_at.isoformat(),
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


def accept_payment(db: Session, params: dict[str, str], settings: Settings, provider_key: str | None = None):
    candidates = []
    for adapter in PAYMENT_ADAPTERS.values():
        if provider_key is not None and adapter.provider_key != provider_key:
            continue
        order_no = adapter.notification_order_no(params)
        if re.fullmatch(r"[a-f0-9]{32}", order_no):
            row = db.scalar(select(RechargeOrder).where(
                RechargeOrder.id == UUID(hex=order_no), RechargeOrder.provider_key == adapter.provider_key,
            ).with_for_update().execution_options(populate_existing=True))
            if row is not None:
                candidates.append((adapter, row))
    if len(candidates) != 1:
        raise HTTPException(400, "fail")
    adapter, order = candidates[0]
    settings = adapter.settings_for_order(db, order, settings)
    merchant, amount, platform_id = adapter.verify_notification(params, settings)
    if (
        order is None or order.merchant_num != merchant
        or Decimal(amount) * 100 != order.amount_fen
        or (adapter.provider_key == EZFP_PROVIDER_KEY and params.get("type") != order.pay_type)
        or (order.platform_order_no is not None and order.platform_order_no != platform_id)
    ):
        raise HTTPException(400, "fail")
    if order.status == "paid":
        db.commit()
        return

    # Order, balance, ledger and audit commit together, including late notifications.
    entry = recharge(
        db, order.workspace_id, order.user_id, order.credits, order.id, order.amount_fen,
    )
    order.platform_order_no = platform_id
    order.status = "paid"
    order.paid_at = now()
    record_audit(
        db, workspace_id=order.workspace_id, actor_user_id=order.user_id,
        action="recharge.paid", resource_type="recharge_order", resource_id=order.id,
        metadata={"amount_fen": order.amount_fen, "credits": order.credits, "ledger_id": str(entry.id)},
    )
    db.commit()


async def callback_params(request: Request) -> dict[str, str]:
    raw = request.scope.get("query_string", b"")
    if len(raw) > 8192:
        raise ValueError("oversized callback")
    if request.method == "POST":
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("unexpected content type")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise ValueError("oversized callback")
        if raw and body:
            raise ValueError("ambiguous callback")
        raw = bytes(body) or raw
    pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, errors="strict", max_num_fields=30)
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate callback parameters")
    return dict(pairs)


def build_router(get_db):
    router = APIRouter(prefix="/payments", tags=["payments"])

    @router.get("/catalog")
    def catalog(db: Session = Depends(get_db), access: Access = Depends(get_access)):
        try:
            channels = []
            for provider_key in PAYMENT_PROVIDERS:
                try:
                    provider_settings, _ = effective_settings(db, provider_key=provider_key)
                    packages, custom = recharge_options(provider_settings, provider_key)
                    if packages:
                        channels.append({"provider_key": provider_key, "name": PAYMENT_PROVIDERS[provider_key]["name"],
                            "packages": [{**item.model_dump(), "quote": package_quote(item)} for item in packages],
                            **({"custom": custom} if custom else {})})
                except HTTPException:
                    # An unavailable channel must not hide other independently configured providers.
                    continue
        except HTTPException:
            channels = []
        if not channels:
            return {"enabled": False, "packages": []}
        return {"enabled": True, "providers": channels, "packages": channels[0]["packages"],
                **({"custom": channels[0]["custom"]} if "custom" in channels[0] else {})}

    @router.post("/orders", status_code=201)
    def create_order(
        payload: CreateOrder, request: Request,
        db: Session = Depends(get_db), access: Access = Depends(get_access),
    ):
        auth.rate_limit(request, "recharge", str(access.user_id), limit=20)
        # Serialize idempotency checks even before a first order exists.
        db.scalar(select(Workspace).where(Workspace.id == access.workspace_id).with_for_update())
        order = db.scalar(select(RechargeOrder).where(
            RechargeOrder.workspace_id == access.workspace_id,
            RechargeOrder.user_id == access.user_id,
            RechargeOrder.idempotency_key == payload.idempotency_key,
        ))
        if order:
            requested_id = (f"{payload.package_id}:{payload.custom_amount_fen}"
                            if payload.custom_amount_fen is not None else payload.package_id)
            if order.package_id != requested_id or order.provider_key != payload.provider_key:
                raise HTTPException(409, "此充值请求已使用其他档位，请重新选择")
            result = order_read(order)
            db.commit()
            return result
        adapter = payment_adapter(payload.provider_key)
        settings, config_version = effective_settings(db, provider_key=adapter.provider_key)
        package = resolve_package(settings, payload, adapter.provider_key)
        merchant = settings.ezfp_pid if adapter.provider_key == EZFP_PROVIDER_KEY else settings.zhifux_merchant_num
        pay_type = settings.ezfp_pay_type if adapter.provider_key == EZFP_PROVIDER_KEY else settings.zhifux_pay_type
        order = RechargeOrder(
            id=uuid4(), workspace_id=access.workspace_id, user_id=access.user_id,
            idempotency_key=payload.idempotency_key,
            package_id=f"{package.id}:{package.amount_fen}" if payload.custom_amount_fen is not None else package.id,
            package_name=package.name,
            amount_fen=package.amount_fen, credits=package.credits,
            merchant_num=merchant, pay_type=pay_type,
            provider_key=adapter.provider_key, payment_config_version=config_version,
            status="creating", expires_at=now() + timedelta(minutes=PAY_DURATION_MINUTES),
        )
        db.add(order)
        db.flush()
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="recharge.created", resource_type="recharge_order", resource_id=order.id,
            metadata={"amount_fen": order.amount_fen, "credits": order.credits},
        )
        db.commit()
        order_id = order.id
        try:
            client_ip = request.client.host if request.client else ""
            checkout = adapter.create_checkout(settings, order, client_ip)
            next_status = "pending"
        except GatewayError as exc:
            checkout = None
            next_status = "failed" if exc.rejected else "confirming"
        # Do not let the gateway response overwrite an earlier successful callback.
        db.expire_all()
        order = db.scalar(select(RechargeOrder).where(
            RechargeOrder.id == order_id,
        ).with_for_update().execution_options(populate_existing=True))
        if order.status != "paid":
            if checkout:
                if order.platform_order_no and order.platform_order_no != checkout.platform_order_no:
                    next_status = "confirming"
                else:
                    order.platform_order_no = checkout.platform_order_no
                    order.pay_url = checkout.pay_url
            order.status = next_status
        db.commit()
        access.revalidate(db)
        return order_read(order)

    @router.get("/orders")
    def list_orders(
        page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=50),
        db: Session = Depends(get_db), access: Access = Depends(get_access),
    ):
        filters = (
            RechargeOrder.workspace_id == access.workspace_id,
            RechargeOrder.user_id == access.user_id,
        )
        total = db.scalar(select(func.count()).select_from(RechargeOrder).where(*filters))
        rows = db.scalars(select(RechargeOrder).where(*filters).order_by(
            RechargeOrder.created_at.desc(), RechargeOrder.id.desc(),
        ).offset((page - 1) * page_size).limit(page_size)).all()
        return {"items": [order_read(item) for item in rows], "total": total, "page": page, "page_size": page_size}

    @router.get("/orders/{order_id}")
    def read_order(order_id: UUID, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        row = db.scalar(select(RechargeOrder).where(
            RechargeOrder.id == order_id,
            RechargeOrder.workspace_id == access.workspace_id,
            RechargeOrder.user_id == access.user_id,
        ))
        if row is None:
            raise HTTPException(404, "充值订单不存在")
        return order_read(row)

    async def notify_callback(request: Request, db: Session, provider_key: str):
        try:
            params = await callback_params(request)
            await run_in_threadpool(accept_payment, db, params, get_settings(), provider_key)
        except HTTPException as exc:
            db.rollback()
            return PlainTextResponse("fail", status_code=exc.status_code)
        except ValueError:
            db.rollback()
            return PlainTextResponse("fail", status_code=400)
        except SQLAlchemyError:
            db.rollback()
            return PlainTextResponse("fail", status_code=503)
        return PlainTextResponse("success")

    @router.api_route("/zhifux/notify", methods=["GET", "POST"], response_class=PlainTextResponse)
    async def notify_zhifux(request: Request, db: Session = Depends(get_db)):
        return await notify_callback(request, db, PROVIDER_KEY)

    @router.api_route("/ezfp/notify", methods=["GET", "POST"], response_class=PlainTextResponse)
    async def notify_ezfp(request: Request, db: Session = Depends(get_db)):
        return await notify_callback(request, db, EZFP_PROVIDER_KEY)

    return router
