"""Product suggestions, separate from answer ratings and chat contents."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .auth import get_access
from .db import get_db
from .models import ProductSuggestion, User
from .site_auth import site_admin
from .site_audit import record

router = APIRouter(tags=["suggestions"])
Category = Literal["feature", "problem", "other"]
Status = Literal["pending", "processing", "resolved"]


class SuggestionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    category: Category
    content: str = Field(min_length=1, max_length=2000)


class StatusWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
    version: int = Field(ge=1, strict=True)


@router.post("/suggestions", status_code=201)
def submit(payload: SuggestionWrite, db=Depends(get_db), access=Depends(get_access)):
    # A per-user lock serializes both retry deduplication and the account-wide quota.
    db.scalar(select(User).where(User.id == access.user_id).with_for_update())
    existing = db.scalar(select(ProductSuggestion).where(
        ProductSuggestion.user_id == access.user_id, ProductSuggestion.request_id == payload.request_id))
    if existing:
        if (existing.category, existing.content) != (payload.category, payload.content):
            raise HTTPException(409, "本次反馈已提交，请重新打开窗口提交新建议")
        return {"id": str(existing.id)}
    recent = db.scalar(select(func.count()).select_from(ProductSuggestion).where(
        ProductSuggestion.user_id == access.user_id,
        ProductSuggestion.created_at >= datetime.now(timezone.utc) - timedelta(minutes=1)))
    if recent >= 3:
        raise HTTPException(429, "提交较频繁，请稍后再试")
    row = ProductSuggestion(user_id=access.user_id, account_id=access.user_id, **payload.model_dump())
    db.add(row)
    db.commit()
    return {"id": str(row.id)}


def read(db, row, detail=False):
    user = db.get(User, row.user_id)
    result = {"id": str(row.id), "category": row.category, "status": row.status,
        "summary": row.content[:120], "user_name": user.name, "user_id": str(row.user_id),
        "created_at": row.created_at, "version": row.version, "handled_at": row.handled_at}
    if detail:
        handler = db.get(User, row.handled_by) if row.handled_by else None
        result.update(content=row.content, handled_by=handler.name if handler else None)
    return result


@router.get("/site/admin/suggestions")
def listing(q: str = Query(default="", max_length=200), category: Category | None = None,
            status: Status | None = None, page: int = Query(default=1, ge=1),
            page_size: int = Query(default=25, ge=1, le=100), db=Depends(get_db), actor=Depends(site_admin)):
    filters = []
    if q.strip():
        filters.append(ProductSuggestion.content.icontains(q.strip(), autoescape=True))
    if category:
        filters.append(ProductSuggestion.category == category)
    if status:
        filters.append(ProductSuggestion.status == status)
    total = db.scalar(select(func.count()).select_from(ProductSuggestion).where(*filters))
    rows = db.scalars(select(ProductSuggestion).where(*filters)
        .order_by(ProductSuggestion.created_at.desc(), ProductSuggestion.id)
        .offset((page - 1) * page_size).limit(page_size))
    return {"items": [read(db, row) for row in rows], "total": total, "page": page, "page_size": page_size}


def find(db, identifier, lock=False):
    row = db.get(ProductSuggestion, identifier, with_for_update=lock, populate_existing=True)
    if row is None:
        raise HTTPException(404, "反馈不存在")
    return row


@router.get("/site/admin/suggestions/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    return read(db, find(db, identifier), True)


@router.put("/site/admin/suggestions/{identifier}")
def update(identifier: UUID, payload: StatusWrite, request: Request,
           db=Depends(get_db), actor=Depends(site_admin)):
    row = find(db, identifier, True)
    if row.version != payload.version:
        raise HTTPException(409, "反馈状态已更新，请重新加载后再保存")
    row.status, row.version = payload.status, row.version + 1
    row.handled_by, row.handled_at = actor.id, datetime.now(timezone.utc)
    record(db, actor, "site.suggestion_updated", "suggestion", row.id, request=request,
           details={"fields": ["status"], "version": row.version})
    db.commit()
    return read(db, row, True)
