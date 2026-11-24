from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.identity import auth
from app.identity.site_auth import site_admin
from app.infra.db import get_db
from app.models import SiteAuditEvent, User
from app.operations.site_audit import RETENTION_DAYS

router = APIRouter(prefix="/site/admin", tags=["site-admin"])


@router.get("/audit")
def list_site_audit(page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100),
                    action: str | None = Query(default=None, max_length=80),
                    target_type: str | None = Query(default=None, max_length=40),
                    actor_id: UUID | None = None, result: str | None = Query(default=None, pattern="^(success|failed|blocked)$"),
                    from_date: datetime | None = None, to_date: datetime | None = None,
                    db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    filters = [SiteAuditEvent.created_at >= datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)]
    if from_date: from_date = auth.aware(from_date)
    if to_date: to_date = auth.aware(to_date)
    if actor_id: filters.append(SiteAuditEvent.actor_user_id == actor_id)
    if result: filters.append(SiteAuditEvent.result == result)
    if from_date and to_date and from_date >= to_date: raise HTTPException(422, "开始时间必须早于结束时间")
    if action: filters.append(SiteAuditEvent.action == action)
    if target_type: filters.append(SiteAuditEvent.target_type == target_type)
    if from_date: filters.append(SiteAuditEvent.created_at >= from_date)
    if to_date: filters.append(SiteAuditEvent.created_at < to_date)
    total = db.scalar(select(func.count()).select_from(SiteAuditEvent).where(*filters)) or 0
    rows = db.scalars(select(SiteAuditEvent).where(*filters).order_by(SiteAuditEvent.created_at.desc(), SiteAuditEvent.id.desc())
        .offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [{"id": str(row.id), "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "action": row.action, "target_type": row.target_type, "target_id": row.target_id,
        "result": row.result, "details": row.details or {}, "created_at": row.created_at.isoformat()} for row in rows],
        "total": total, "page": page, "page_size": page_size, "retention_days": RETENTION_DAYS}
