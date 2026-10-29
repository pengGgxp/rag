"""Read-only RMB-to-credit quotes using the actual recharge catalog."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import payments
from .db import get_db
from .site_auth import site_admin
from .credit_amounts import amount

router = APIRouter(prefix="/site/admin/pricing-calculator", tags=["site-admin"])
Money = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,8})?$")]


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Literal["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens", "ocr_pages", "embedding_tokens"]
    unit_size: int = Field(ge=1, le=2_147_483_647)
    cost_yuan: Money
    profit_yuan: Money
    overhead_yuan: Money = "0"
    payment_fee_percent: str = Field(default="0.1", pattern=r"^(?:0|[1-9][0-9]?)(?:\.[0-9]{1,4})?$")
    multiplier: str = Field(default="1.00",
        pattern=r"^(?:0\.(?:0[1-9]|[1-9][0-9]?)|[1-9][0-9]?(?:\.[0-9]{1,2})?|100(?:\.0{1,2})?)$")


def decimal_text(value):
    with localcontext() as context:
        context.prec = 70
        result = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return format(result, "f")


def reference_package():
    try:
        packages = payments.configured_packages(payments.get_settings())
    except HTTPException:
        packages = []
    if not packages:
        raise HTTPException(503, "请先配置可用的充值套餐，再按人民币成本计算价格")
    # The most credits per yuan yields the lowest revenue for each consumed credit.
    return max(packages, key=lambda item: Fraction(item.credits * 100, item.amount_fen))


def package_read(package):
    return {"id": package.id, "name": package.name, "amount_fen": package.amount_fen,
        "credits": package.credits, "credits_per_yuan": decimal_text(Fraction(package.credits * 100, package.amount_fen))}


@router.get("")
def read_context(actor=Depends(site_admin)):
    try:
        package = reference_package()
    except HTTPException as error:
        return {"ready": False, "reference_package": None, "reason": error.detail}
    return {"ready": True, "reference_package": package_read(package), "reason": None}


@router.post("/quote")
def quote(payload: QuoteInput, db=Depends(get_db), actor=Depends(site_admin)):
    package = reference_package()
    rate = Fraction(package.credits * 100, package.amount_fen)
    cost = Fraction(payload.cost_yuan) + Fraction(payload.overhead_yuan)
    profit = Fraction(payload.profit_yuan)
    fee = Fraction(payload.payment_fee_percent) / 100
    target = (cost + profit) / (1 - fee)
    credits = amount(target * rate)
    if credits > 2_147_483_647:
        raise HTTPException(422, "换算积分超出价格上限，请减小每单位用量及对应成本")
    base_revenue = Fraction(credits) / rate
    effective_credits = Fraction(credits) * Fraction(payload.multiplier)
    revenue = effective_credits / rate
    actual_fee = revenue * fee
    actual_profit = revenue - actual_fee - cost
    return {"unit_size": payload.unit_size, "credits_per_unit": credits,
        "reference_package": package_read(package), "target_price_yuan": decimal_text(target),
        "base_price_yuan": decimal_text(base_revenue), "rounding_extra_yuan": decimal_text(base_revenue - target),
        "effective_unit_credits": decimal_text(effective_credits),
        "effective_revenue_yuan": decimal_text(revenue), "effective_fee_yuan": decimal_text(actual_fee),
        "effective_profit_yuan": decimal_text(actual_profit),
        "target_met": actual_profit >= profit, "loss": actual_profit < 0,
        "unit_rounding": False}
