from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .identity.auth import Access, get_access
from .db import get_db
from .models import AuditLog, User

router = APIRouter(tags=["audit"])


def _read(row: AuditLog, actor: User | None):
    return {
        "id": str(row.id),
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "metadata": row.metadata_json or {},
        "actor": (
            {"id": str(actor.id), "name": actor.name, "email": actor.email}
            if actor else None
        ),
        "created_at": row.created_at.isoformat(),
    }


@router.get("/audit-logs")
def list_audit_logs(
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=100),
    action: str | None = Query(None, max_length=100),
    resource_type: str | None = Query(None, max_length=50),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    access: Access = Depends(get_access),
):
    conditions = [AuditLog.owner_user_id == access.user_id]
    if action:
        conditions.append(AuditLog.action == action)
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    if from_date:
        conditions.append(AuditLog.created_at >= from_date)
    if to_date:
        conditions.append(AuditLog.created_at < to_date)
    total = db.scalar(select(func.count()).select_from(AuditLog).where(and_(*conditions))) or 0
    rows = db.execute(
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .where(and_(*conditions))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_read(row, actor) for row, actor in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
