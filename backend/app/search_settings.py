"""Independent encrypted web-search configuration and explicit platform prices."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text

from .auth import get_access
from .credit_amounts import CreditRate
from .db import get_db
from .models import SiteSearchSettings
from .site_auth import protect, reveal, site_admin
from .site_audit import record
from .web_search import CAPABILITIES, SearchConfig

router = APIRouter(tags=["search"])


class SearchPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Literal["web_search", "web_fetch"]
    unit: Literal["calls"] = "calls"
    unit_size: Literal[1, 1000] = 1
    credits_per_unit: CreditRate | None = None
    enabled: bool = True


def complete(prices):
    try:
        return set(prices) == set(CAPABILITIES) and all(
            (price := SearchPrice.model_validate(prices[key])).capability == key
            and price.credits_per_unit is not None and price.enabled for key in CAPABILITIES)
    except (TypeError, ValueError, KeyError):
        return False


def read(row):
    return {"version": row.version if row else 0, "enabled": bool(row and row.enabled),
        "has_api_key": bool(row and row.encrypted_api_key),
        **{key: getattr(row, key) if row else default for key, default in (
            ("timeout_seconds", 20), ("result_count", 5), ("max_search_calls", 3), ("max_fetch_calls", 5))},
        "policies": [{**(row.prices.get(key) if row and key in row.prices else
            {"capability": key, "unit": "calls", "unit_size": 1, "credits_per_unit": None, "enabled": True}),
            "version": row.version if row else 0,
            "configured": bool(row and key in row.prices and row.prices[key].get("credits_per_unit") is not None)}
            for key in CAPABILITIES]}


def runtime_config(db):
    row = db.get(SiteSearchSettings, 1, populate_existing=True)
    if not row or not row.enabled or not row.encrypted_api_key or not complete(row.prices):
        return None
    try:
        return SearchConfig(version=row.version, api_key=reveal(row.encrypted_api_key),
            **{key: getattr(row, key) for key in (
                "timeout_seconds", "result_count", "max_search_calls", "max_fetch_calls", "prices")})
    except Exception:
        return None


class SearchSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    enabled: bool
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    timeout_seconds: int = Field(default=20, ge=5, le=60)
    result_count: int = Field(default=5, ge=1, le=10)
    max_search_calls: int = Field(default=3, ge=1, le=10)
    max_fetch_calls: int = Field(default=5, ge=1, le=10)
    policies: list[SearchPrice] = Field(min_length=2, max_length=2)


@router.get("/site/admin/search")
def get_settings(db=Depends(get_db), actor=Depends(site_admin)):
    return read(db.get(SiteSearchSettings, 1))


@router.put("/site/admin/search")
def save_settings(payload: SearchSettingsInput, request: Request, db=Depends(get_db), actor=Depends(site_admin)):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020058)"))
    row = db.get(SiteSearchSettings, 1, with_for_update=True, populate_existing=True)
    if (row.version if row else 0) != payload.version:
        raise HTTPException(409, "联网搜索配置已更新，请重新加载后保存")
    if payload.clear_api_key and payload.api_key and payload.api_key.get_secret_value().strip():
        raise HTTPException(422, "更换与清除密钥不能同时操作")
    if row is None:
        row = SiteSearchSettings(id=1, version=0)
        db.add(row)
    if payload.clear_api_key:
        row.encrypted_api_key = None
    elif payload.api_key and payload.api_key.get_secret_value().strip():
        row.encrypted_api_key = protect(payload.api_key.get_secret_value().strip())
    prices = {price.capability: price.model_dump() for price in payload.policies}
    if len(prices) != 2:
        raise HTTPException(422, "搜索与网页读取价格不能重复")
    if payload.enabled and (not row.encrypted_api_key or not complete(prices)):
        raise HTTPException(422, "启用前请填写密钥及搜索、网页读取的完整价格")
    row.prices = prices
    for key in ("enabled", "timeout_seconds", "result_count", "max_search_calls", "max_fetch_calls"):
        setattr(row, key, getattr(payload, key))
    row.version += 1
    record(db, actor, "search.updated", "search_settings", 1, request=request,
        details={"version": row.version, "fields": list(payload.model_fields_set - {"api_key"})
            + (["api_key"] if payload.api_key else [])})
    db.commit()
    return read(row)


@router.get("/search/availability")
def availability(db=Depends(get_db), access=Depends(get_access)):
    from . import model_billing
    return {"available": bool(model_billing.enabled(db) and runtime_config(db))}
