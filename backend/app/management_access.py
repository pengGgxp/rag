"""Explicit site-admin scope for workspace model and pricing management."""
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_session
from .db import get_db
from .models import LoginSession, User, Workspace
from .site_auth import site_admin
from .site_audit import record


@dataclass(frozen=True)
class ManagementAccess:
    actor: User
    workspace_id: UUID
    request: Request

    @property
    def user_id(self):
        return self.actor.id

    def audit(self, db, action, *, target_id=None, fields=(), result="success"):
        record(db, self.actor, action, "workspace", self.workspace_id,
               request=self.request, result=result,
               details={"workspace_id": str(self.workspace_id),
                        "resource_id": str(target_id) if target_id else None,
                        "fields": sorted(fields)})


def management_access(request: Request, actor: User = Depends(site_admin),
                      session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    # Legacy URLs always use the session workspace; query parameters cannot change the target.
    raw = request.path_params.get("workspace_id")
    try:
        workspace_id = UUID(str(raw)) if raw is not None else session.workspace_id
    except (TypeError, ValueError):
        raise HTTPException(422, "工作区 ID 不合法") from None
    # Serialize management writes/default creation without changing billing snapshots.
    if db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update()) is None:
        raise HTTPException(404, "工作区不存在")
    access = ManagementAccess(actor, workspace_id, request)
    try:
        yield access
    except Exception:
        db.rollback()
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            action = "workspace.pricing_management_failed" if "/pricing" in request.url.path or "/usage" in request.url.path else "workspace.model_management_failed"
            access.audit(db, action, result="failure")
            db.commit()
        raise
