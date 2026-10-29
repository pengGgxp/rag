"""Exact credit arithmetic; one credit contains 10,000 accounting ticks."""
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated

from pydantic import BeforeValidator, Field, PlainSerializer

SCALE = 10_000


def parse_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Credits must be a decimal number")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Credits must be a decimal number") from exc


CreditRate = Annotated[
    Decimal, BeforeValidator(parse_decimal),
    Field(ge=0, le=2_147_483_647, decimal_places=4, allow_inf_nan=False),
    PlainSerializer(lambda value: float(value), return_type=float),
]


def exact_charge(units, price):
    return Fraction(units, price["unit_size"]) * Fraction(str(price["credits_per_unit"]))


def ticks(value):
    exact = Fraction(value) * SCALE
    if exact < 0:
        return -ticks(-Fraction(value))
    whole, remainder = divmod(exact.numerator, exact.denominator)
    return whole + (2 * remainder >= exact.denominator)


def from_ticks(value):
    return Decimal(value) / SCALE


def amount(value):
    """Round half up to four decimals, never ceil a billing unit or whole credit."""
    return from_ticks(ticks(value))


def allocate(parts):
    """Round the request total once, then distribute ticks by largest remainder."""
    scaled = [Fraction(part) * SCALE for part in parts]
    total = ticks(sum(parts, Fraction()))
    allocated = [part.numerator // part.denominator for part in scaled]
    ranked = sorted(range(len(parts)), key=lambda i: (-(scaled[i] - allocated[i]), i))
    for index in ranked[:total - sum(allocated)]:
        allocated[index] += 1
    return from_ticks(total), [from_ticks(value) for value in allocated]
