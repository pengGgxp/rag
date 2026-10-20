from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog


def record_audit(
    db: Session,
    *,
    workspace_id: UUID,
    action: str,
    resource_type: str,
    actor_user_id: UUID | None = None,
    resource_id: UUID | None = None,
    metadata: dict | None = None,
    request: Request | None = None,
):
    client = request.client if request else None
    user_agent = request.headers.get("user-agent", "")[:500] if request else None
    db.add(AuditLog(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_json=metadata or {},
        ip_address=client.host[:64] if client else None,
        user_agent=user_agent,
    ))
