"""Deployment maintenance state and loopback control endpoint."""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal, get_db
from .models import DeploymentState

router = APIRouter(prefix="/internal/deploy", tags=["deployment"])
LOGGER = logging.getLogger(__name__)


class MaintenanceUpdate(BaseModel):
    active: bool
    release: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=300)


def _row(db: Session) -> DeploymentState:
    row = db.get(DeploymentState, 1)
    if row is None:
        row = DeploymentState(id=1, active=False, version=1)
        db.add(row)
        db.flush()
    return row


def is_active(factory: Callable[[], Session] = SessionLocal) -> bool:
    """Fail closed if maintenance state cannot be verified."""
    try:
        with factory() as db:
            row = db.get(DeploymentState, 1)
            return bool(row and row.active)
    except SQLAlchemyError:
        LOGGER.exception("Unable to read deployment maintenance state")
        return True


def _loopback(host: str | None) -> bool:
    try:
        return bool(host) and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_control(request: Request) -> None:
    host = request.client.host if request.client else None
    settings = get_settings()
    configured = settings.deploy_control_token
    supplied = request.headers.get("authorization", "")
    expected = (
        "Bearer " + configured.get_secret_value()
        if configured and configured.get_secret_value()
        else ""
    )
    if not expected:
        raise HTTPException(503, "deployment control is not configured")
    if not _loopback(host):
        raise HTTPException(404, "Not Found")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid deployment control token")


def _response(row: DeploymentState) -> dict:
    return {
        "active": row.active,
        "release": row.release,
        "message": row.message,
        "version": row.version,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/maintenance", dependencies=[Depends(require_control)])
def maintenance_status(db: Session = Depends(get_db)):
    row = _row(db)
    db.commit()
    return _response(row)


@router.put("/maintenance", dependencies=[Depends(require_control)])
def set_maintenance(payload: MaintenanceUpdate, db: Session = Depends(get_db)):
    row = _row(db)
    row.active = payload.active
    row.release = payload.release
    row.message = payload.message
    row.version += 1
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    LOGGER.warning(
        "Deployment maintenance changed active=%s release=%s version=%s",
        row.active,
        row.release,
        row.version,
    )
    return _response(row)


def _mutation_allowed(path: str) -> bool:
    if path.startswith("/internal/deploy/"):
        return True
    if path in {"/payments/zhifux/notify", "/payments/ezfp/notify"}:
        return True
    if re.fullmatch(r"/payments/(?:orders|order-attempts)/[^/]+", path):
        return True
    if path in {"/auth/login", "/auth/logout", "/auth/workspace"}:
        return True
    if re.fullmatch(r"/conversations/[^/]+/stop", path):
        return True
    if re.fullmatch(r"/cloud-computers/[^/]+/end", path):
        return True
    if re.fullmatch(r"/cloud-computer/commands/[^/]+/cancel", path):
        return True
    if re.fullmatch(r"/previews/[^/]+/(access|heartbeat|stop)", path):
        return True
    if re.fullmatch(r"/image-tasks/[^/]+/cancel", path):
        return True
    return False


class DeploymentMaintenanceMiddleware:
    """Pause new writes while allowing safe reads and cost-stopping actions."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
            and not _mutation_allowed(scope.get("path", ""))
            and is_active()
        ):
            response = JSONResponse(
                {"detail": "系统正在进行部署维护，请稍后重试"},
                status_code=503,
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
