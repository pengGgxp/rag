"""Cross-workspace site administration APIs, separate from workspace roles."""
from datetime import datetime, timedelta, timezone
import secrets
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, delete, func, or_, select
from sqlalchemy.orm import Session

from . import auth
from .db import get_db
from .email_auth import email_rate_limit
from .models import (
    CreditAccount, CreditLedgerEntry, ExternalIdentity, LLMProviderConfig, LoginSession, Membership,
    SiteAuditEvent, SiteModelProvider, User, Workspace,
)
from .site_auth import site_admin, send_mail
from .site_audit import record, RETENTION_DAYS

router = APIRouter(prefix="/site/admin", tags=["site-admin"])


class CreditAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    direction: Literal["add", "deduct"]
    amount: int = Field(ge=1, le=2_147_483_647, strict=True)
    expected_balance: int = Field(ge=0, le=2_147_483_647, strict=True)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请填写调整原因")
        return value


def credit_values(account):
    return {key: getattr(account, key) if account else 0
            for key in ("balance", "lifetime_granted", "lifetime_spent")}


def adjustment_read(entry, actor_name=None):
    return {"id": str(entry.id), "amount": entry.amount, "balance_after": entry.balance_after,
        "reason": entry.metadata_json.get("reason", ""), "actor_name": actor_name,
        "actor_user_id": str(entry.actor_user_id) if entry.actor_user_id else None,
        "created_at": entry.created_at.isoformat()}


@router.get("/workspaces/{workspace_id}/credits")
def read_workspace_credits(workspace_id: UUID, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    if db.get(Workspace, workspace_id) is None:
        raise HTTPException(404, "工作区不存在")
    from .model_billing import pending_credits
    entries = db.execute(select(CreditLedgerEntry, User.name).outerjoin(User,
        User.id == CreditLedgerEntry.actor_user_id).where(CreditLedgerEntry.workspace_id == workspace_id,
        CreditLedgerEntry.kind.in_(("manual_credit", "manual_debit")))
        .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id.desc()).limit(30)).all()
    return {"credits": credit_values(db.get(CreditAccount, workspace_id)),
        "pending_credits": pending_credits(db, workspace_id),
        "adjustments": [adjustment_read(entry, name) for entry, name in entries]}


@router.post("/workspaces/{workspace_id}/credits/adjustments")
def adjust_workspace_credits(workspace_id: UUID, payload: CreditAdjustment, request: Request,
                             db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    from .credits import adjust_balance
    if db.get(Workspace, workspace_id) is None:
        raise HTTPException(404, "工作区不存在")
    signed = payload.amount if payload.direction == "add" else -payload.amount
    account, entry, created = adjust_balance(db, workspace_id, actor.id, payload.request_id,
        signed, payload.reason, payload.expected_balance)
    if created:
        record(db, actor, "workspace.credits_adjusted", "workspace", workspace_id, request=request,
            details={"request_id": str(payload.request_id), "resource_id": str(entry.id), "amount": signed,
                     "balance_before": entry.balance_after - signed, "balance_after": entry.balance_after})
    db.commit()
    return {"credits": credit_values(account), "adjustment": adjustment_read(entry, actor.name)}


def reject_operation(db, actor, action, user, request, status, message):
    record(db, actor, action, "user", user.id, request=request, result="blocked")
    db.commit()
    raise HTTPException(status, message)

def search_term(value):
    return "%" + value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def user_summary(db: Session, user: User):
    memberships = db.execute(select(Workspace.id, Workspace.name, Membership.role, CreditAccount.balance,
        CreditAccount.lifetime_granted, CreditAccount.lifetime_spent)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .outerjoin(CreditAccount, CreditAccount.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id).order_by(Workspace.created_at)).all()
    identities = db.scalars(select(ExternalIdentity.provider).where(ExternalIdentity.user_id == user.id)).all()
    return {
        "id": str(user.id), "email": user.email, "name": user.name,
        "email_verified": user.email_verified_at is not None,
        "email_verified_at": user.email_verified_at.isoformat() if user.email_verified_at else None,
        "is_site_admin": user.is_site_admin, "is_suspended": user.is_suspended,
        "created_at": user.created_at.isoformat(),
        "login_methods": {"password": user.password_hash is not None,
            "wechat": any(provider.startswith("senyu:") for provider in identities),
            "other_oauth": any(not provider.startswith("senyu:") for provider in identities)},
        "active_sessions": db.scalar(select(func.count()).select_from(LoginSession).where(
            LoginSession.user_id == user.id, LoginSession.expires_at > datetime.now(timezone.utc))) or 0,
        "workspaces": [{"id": str(row.id), "name": row.name, "role": row.role,
            "credits": {"balance": row.balance or 0, "lifetime_granted": row.lifetime_granted or 0,
                           "lifetime_spent": row.lifetime_spent or 0}} for row in memberships],
    }


def workspace_summary(db: Session, workspace: Workspace):
    account = db.get(CreditAccount, workspace.id)
    providers = db.scalars(select(SiteModelProvider.name).join(LLMProviderConfig,
        LLMProviderConfig.site_provider_id == SiteModelProvider.id).where(
        LLMProviderConfig.workspace_id == workspace.id, SiteModelProvider.enabled.is_(True))).all()
    return {
        "id": str(workspace.id), "name": workspace.name, "created_at": workspace.created_at.isoformat(),
        "member_count": db.scalar(select(func.count()).select_from(Membership).where(
            Membership.workspace_id == workspace.id)) or 0,
        "credits": {"balance": account.balance if account else 0,
            "lifetime_granted": account.lifetime_granted if account else 0,
            "lifetime_spent": account.lifetime_spent if account else 0},
        "configuration": {"enabled_model_providers": len(providers),
            "provider_names": list(providers)},
        "billing_multiplier": f"{workspace.billing_multiplier_percent // 100}.{workspace.billing_multiplier_percent % 100:02d}",
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    now = datetime.now(timezone.utc)
    return {
        "users_total": db.scalar(select(func.count()).select_from(User)) or 0,
        "users_registered_30d": db.scalar(select(func.count()).select_from(User).where(
            User.created_at >= now - timedelta(days=30))) or 0,
        "users_unverified": db.scalar(select(func.count()).select_from(User).where(
            User.email_verified_at.is_(None))) or 0,
        "users_suspended": db.scalar(select(func.count()).select_from(User).where(
            User.is_suspended.is_(True))) or 0,
        "workspaces_total": db.scalar(select(func.count()).select_from(Workspace)) or 0,
    }


@router.get("/users")
def list_users(q: str = Query(default="", max_length=200), verified: str = Query(default="all"),
               status: str = Query(default="all"), page: int = Query(default=1, ge=1),
               page_size: int = Query(default=25, ge=1, le=100), db: Session = Depends(get_db),
               actor: User = Depends(site_admin)):
    filters = []
    if q.strip():
        term = search_term(q)
        filters.append(or_(User.email.ilike(term, escape="\\"), User.name.ilike(term, escape="\\"), User.id.cast(String).ilike(term, escape="\\")))
    if verified == "yes": filters.append(User.email_verified_at.is_not(None))
    elif verified == "no": filters.append(User.email_verified_at.is_(None))
    elif verified != "all": raise HTTPException(422, "邮箱验证筛选值无效")
    if status == "active": filters.append(User.is_suspended.is_(False))
    elif status == "suspended": filters.append(User.is_suspended.is_(True))
    elif status != "all": raise HTTPException(422, "账号状态筛选值无效")
    total = db.scalar(select(func.count()).select_from(User).where(*filters)) or 0
    rows = db.scalars(select(User).where(*filters).order_by(User.created_at.desc(), User.id).offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [user_summary(db, row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/users/{user_id}")
def get_user(user_id: UUID, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "用户不存在")
    return user_summary(db, user)


class UserStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspended: bool
    reason_code: str = Field(default="support_review", pattern=r"^(support_review|abuse|security|resolved)$")


@router.put("/users/{user_id}/status")
def set_user_status(user_id: UUID, payload: UserStatus, request: Request,
                    db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user: raise HTTPException(404, "用户不存在")
    if user.is_site_admin and payload.suspended:
        record(db, actor, "user.suspended", "user", user.id, request=request, result="blocked", details={"reason_code": "security"})
        db.commit()
        raise HTTPException(409, "请先通过服务器命令撤销该账号的站点管理员权限")
    if user.is_suspended != payload.suspended:
        user.is_suspended = payload.suspended
        revoked = 0
        if payload.suspended:
            revoked = db.execute(delete(LoginSession).where(LoginSession.user_id == user.id)).rowcount or 0
        record(db, actor, "user.suspended" if payload.suspended else "user.reactivated", "user", user.id,
               request=request, details={"reason_code": payload.reason_code, "previous_suspended": not payload.suspended, "suspended": payload.suspended, "sessions_revoked": revoked})
        db.commit()
    return user_summary(db, user)


@router.post("/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: UUID, request: Request, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user: raise HTTPException(404, "用户不存在")
    revoked = db.execute(delete(LoginSession).where(LoginSession.user_id == user.id)).rowcount or 0
    record(db, actor, "user.sessions_revoked", "user", user.id, request=request, details={"count": revoked})
    db.commit()
    return {"revoked": revoked}


@router.post("/users/{user_id}/resend-verification")
def resend_verification(user_id: UUID, request: Request, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user: raise HTTPException(404, "用户不存在")
    if user.email_verified_at: reject_operation(db, actor, "user.verification_resent", user, request, 409, "邮箱已经验证")
    if user.is_suspended: reject_operation(db, actor, "user.verification_resent", user, request, 409, "停用账号不能重发验证邮件")
    from .site_auth import email_enabled
    if not email_enabled(db): reject_operation(db, actor, "user.verification_resent", user, request, 503, "站点邮件服务尚未启用")
    try:
        email_rate_limit(request, db, user.email, f"site-admin-verify:{user.id}")
        auth.rate_limit(request, "admin-verification", str(user.id), limit=3)
        raw = secrets.token_urlsafe(48)
        from .models import AuthToken
        previous = db.scalar(select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == "site_verify"))
        if previous and auth.aware(previous.expires_at) > auth.now()+timedelta(minutes=29):
            raise HTTPException(429, "请等待 60 秒后重新发送")
        db.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == "site_verify"))
        db.add(AuthToken(token_hash=auth.digest(raw), purpose="site_verify", user_id=user.id,
                         expires_at=auth.now()+timedelta(minutes=30)))
        url = f"{auth.get_settings().frontend_url.rstrip('/')}/auth/confirm-email#token={raw}"
        from html import escape
        send_mail(db, user.email, "知库 - 验证邮箱", f"请在 30 分钟内打开链接完成邮箱验证：\n{url}\n如果不是你本人操作，请忽略此邮件。",
                  f'<h1>验证知库邮箱</h1><p><a href="{escape(url, quote=True)}">验证邮箱</a></p><p>30 分钟内有效。非本人操作请忽略。</p>')
        record(db, actor, "user.verification_resent", "user", user.id, request=request)
        db.commit()
    except Exception:
        db.rollback()
        record(db, actor, "user.verification_resent", "user", user.id, request=request, result="failed")
        db.commit()
        raise
    return {"message": "验证邮件已提交发送，请用户检查收件箱及垃圾邮件"}


@router.get("/workspaces")
def list_workspaces(q: str = Query(default="", max_length=200), page: int = Query(default=1, ge=1),
                    page_size: int = Query(default=25, ge=1, le=100), db: Session = Depends(get_db),
                    actor: User = Depends(site_admin)):
    filters = [or_(Workspace.name.ilike(search_term(q), escape="\\"), Workspace.id.cast(String).ilike(search_term(q), escape="\\"))] if q.strip() else []
    total = db.scalar(select(func.count()).select_from(Workspace).where(*filters)) or 0
    rows = db.scalars(select(Workspace).where(*filters).order_by(Workspace.created_at.desc(), Workspace.id).offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [workspace_summary(db, row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: UUID, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    workspace = db.get(Workspace, workspace_id)
    if not workspace: raise HTTPException(404, "工作区不存在")
    members = db.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id)
        .where(Membership.workspace_id == workspace.id).order_by(User.created_at)).all()
    summary = workspace_summary(db, workspace)
    summary["members"] = [{"id": str(user.id), "email": user.email, "name": user.name,
        "role": role, "is_suspended": user.is_suspended} for user, role in members]
    return summary


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
