"""Server-owned recharge orders and the payment FM protocol."""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import auth
from .audit_log import record_audit
from .auth import Access, aware, get_access, now
from .config import Settings, get_settings
from .credits import recharge
from .models import RechargeOrder, Workspace

CALLBACK_PATH = "/payments/zhifux/notify"
PAY_DURATION_MINUTES = 15


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


def package_quote(package: RechargePackage) -> str:
    return hashlib.sha256(package.model_dump_json().encode("utf-8")).hexdigest()


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


def configured_packages(settings: Settings) -> list[RechargePackage]:
    if not settings.zhifux_enabled:
        return []
    try:
        packages = TypeAdapter(list[RechargePackage]).validate_json(settings.recharge_packages_json)
        roots = [settings.zhifux_api_base_url, settings.api_public_url, settings.frontend_url]
        valid = (
            1 <= len(packages) <= 20
            and len({item.id for item in packages}) == len(packages)
            and all(https_url(root) and not urlsplit(root).query for root in roots)
            and len(settings.api_public_url.rstrip("/") + CALLBACK_PATH) <= 200
            and len(settings.frontend_url.rstrip("/") + "/?account=1&billing=1") <= 200
            and bool(re.fullmatch(r"[a-zA-Z0-9]{1,100}", settings.zhifux_merchant_num))
            and bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", settings.zhifux_pay_type))
            and settings.zhifux_secret
            and settings.zhifux_secret.get_secret_value()
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


def accept_payment(db: Session, params: dict[str, str], settings: Settings):
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
    order = db.scalar(select(RechargeOrder).where(
        RechargeOrder.id == UUID(hex=order_no),
    ).with_for_update().execution_options(populate_existing=True))
    if (
        order is None or order.merchant_num != merchant
        or Decimal(amount) * 100 != order.amount_fen
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
    def catalog(access: Access = Depends(get_access)):
        try:
            packages = configured_packages(get_settings())
        except HTTPException:
            packages = []
        return {
            "enabled": bool(packages),
            "packages": [{**item.model_dump(), "quote": package_quote(item)} for item in packages],
        }

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
            if order.package_id != payload.package_id:
                raise HTTPException(409, "此充值请求已使用其他档位，请重新选择")
            result = order_read(order)
            db.commit()
            return result
        settings = get_settings()
        package = next((item for item in configured_packages(settings) if item.id == payload.package_id), None)
        if package is None:
            raise HTTPException(409, "该充值档位已不可用，请刷新后重试")
        if payload.quote != package_quote(package):
            raise HTTPException(409, "充值档位已更新，请刷新并确认价格后重试")
        order = RechargeOrder(
            id=uuid4(), workspace_id=access.workspace_id, user_id=access.user_id,
            idempotency_key=payload.idempotency_key, package_id=package.id, package_name=package.name,
            amount_fen=package.amount_fen, credits=package.credits,
            merchant_num=settings.zhifux_merchant_num, pay_type=settings.zhifux_pay_type,
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
            checkout = start_checkout(settings, order)
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

    @router.api_route("/zhifux/notify", methods=["GET", "POST"], response_class=PlainTextResponse)
    async def notify(request: Request, db: Session = Depends(get_db)):
        try:
            params = await callback_params(request)
            await run_in_threadpool(accept_payment, db, params, get_settings())
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

    return router
