"""Opaque browser sessions and workspace authorization."""
import hashlib
import secrets
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Literal
from uuid import UUID

import redis
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pwdlib import PasswordHash
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .audit_log import record_audit
from .models import (
    AuthToken, Conversation, CreditAccount, CreditLedgerEntry, ExternalIdentity,
    KnowledgeBase, KnowledgeBaseStatus, LibraryGrant, LoginSession, Membership,
    QueryTrace, User, Workspace, LEGACY_WORKSPACE_ID,
)

COOKIE = "rag_session"
passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))
router = APIRouter(prefix="/auth", tags=["accounts"])


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def rate_limit(request: Request, action: str, identifier: str = "", limit: int = 15):
    key = "auth-limit:" + digest(f"{action}:{request.client.host if request.client else ''}:{identifier}")
    try:
        store = redis.Redis.from_url(get_settings().redis_url)
        count = store.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],900) end; return n",
            1, key,
        )
    except redis.RedisError as exc:
        raise HTTPException(503, "登录服务暂不可用，请稍后重试") from exc
    if count > limit:
        raise HTTPException(429, "操作过于频繁，请 15 分钟后重试")


class BrowserRequestGuard:
    """Custom header + exact Origin allowlist also protects anonymous login endpoints."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only the signed provider callback is exempt from browser-only headers.
        payment_callback = scope.get("path") == "/payments/zhifux/notify" and scope.get("method") == "POST"
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS") and not payment_callback:
            headers = dict(scope["headers"])
            settings = get_settings()
            origin = headers.get(b"origin", b"").decode()
            allowed = {settings.frontend_url.rstrip("/"), settings.api_public_url.rstrip("/")}
            if headers.get(b"x-rag-request") != b"1" or (origin and origin not in allowed):
                response = JSONResponse({"detail": "请求来源校验失败"}, status_code=403)
                await response(scope, receive, send)
                return
        async def no_store(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(key, value) for key, value in headers if key.lower() != b"cache-control"]
                headers.extend([(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")])
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, no_store)


def current_session(request: Request, db: Session = Depends(get_db)) -> LoginSession:
    raw = request.cookies.get(COOKIE, "")
    session = db.get(LoginSession, digest(raw), populate_existing=True) if raw else None
    if session is None or aware(session.expires_at) <= now():
        raise HTTPException(401, "请先登录")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf_token):
            raise HTTPException(403, "登录校验已失效，请刷新页面")
    return session


@dataclass
class Access:
    user_id: UUID
    workspace_id: UUID
    role: str
    session_hash: str

    def visible_libraries(self):
        return (
            (KnowledgeBase.workspace_id == self.workspace_id)
            & (True if self.role == "admin" else or_(
                KnowledgeBase.restricted.is_(False),
                exists(select(LibraryGrant.user_id).where(
                    LibraryGrant.knowledge_base_id == KnowledgeBase.id,
                    LibraryGrant.user_id == self.user_id,
                )),
            ))
        )

    def library(self, db: Session, id: UUID, write=False, allow_archived=False):
        library = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == id, self.visible_libraries()))
        if library is None:
            raise HTTPException(404, "知识库不存在或无权访问")
        if library.status == KnowledgeBaseStatus.ARCHIVED and not allow_archived:
            if write:
                raise HTTPException(409, "知识库已归档，请先恢复后再修改")
        if write:
            self.editor()
        return library

    def editor(self):
        if self.role not in ("admin", "editor"):
            raise HTTPException(403, "只读成员不能修改资料")

    def admin(self):
        if self.role != "admin":
            raise HTTPException(403, "需要工作区管理员权限")

    def conversation(self, db: Session, row: Conversation):
        if row.owner_id != self.user_id or row.workspace_id != self.workspace_id:
            raise HTTPException(404, "会话不存在或无权访问")
        for scope in row.libraries:
            self.library(db, scope.knowledge_base_id)

    def revalidate(self, db: Session):
        # Fresh sessions are used by streaming generators, so revocation is observed.
        session = db.get(LoginSession, self.session_hash, populate_existing=True)
        member = db.get(Membership, (self.workspace_id, self.user_id), populate_existing=True)
        if session is None or aware(session.expires_at) <= now() or member is None:
            raise HTTPException(403, "登录或工作区权限已失效")
        return Access(self.user_id, self.workspace_id, member.role, self.session_hash)


def get_access(session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    member = db.get(Membership, (session.workspace_id, session.user_id), populate_existing=True)
    if member is None:
        raise HTTPException(403, "你已不在当前工作区，请切换工作区")
    return Access(session.user_id, session.workspace_id, member.role, session.token_hash)


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class Registration(Credentials):
    model_config = ConfigDict(str_strip_whitespace=False)
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1, max_length=100)


class Bootstrap(Registration):
    token: str = Field(min_length=20, max_length=200)


def normalize_email(email):
    return str(email).strip().lower()


def create_user(db, email, name, password=None):
    name = name.strip()
    if not name:
        raise HTTPException(422, "请输入姓名")
    user = User(email=normalize_email(email), name=name, password_hash=passwords.hash(password) if password else None)
    db.add(user)
    db.flush()
    workspace = Workspace(name=f"{name}的工作区"[:100])
    db.add(workspace)
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
    db.add(CreditAccount(
        workspace_id=workspace.id,
        balance=1000,
        lifetime_granted=1000,
        lifetime_spent=0,
    ))
    db.add(CreditLedgerEntry(
        workspace_id=workspace.id,
        actor_user_id=None,
        amount=1000,
        balance_after=1000,
        kind="initial_grant",
        idempotency_key=f"workspace:{workspace.id}:initial",
        metadata_json={"reason": "workspace_created"},
    ))
    db.flush()
    return user, workspace.id


def session_info(db, session):
    user = db.get(User, session.user_id)
    rows = db.execute(select(Workspace, Membership.role).join(
        Membership, Membership.workspace_id == Workspace.id,
    ).where(Membership.user_id == user.id).order_by(Workspace.created_at)).all()
    return {
        "user": {"id": str(user.id), "name": user.name, "email": user.email,
                 "has_password": user.password_hash is not None,
                 "oauth_linked": bool(db.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)))},
        "workspaces": [{"id": str(workspace.id), "name": workspace.name, "role": role} for workspace, role in rows],
        "workspace_id": str(session.workspace_id), "csrf_token": session.csrf_token,
    }


def login_response(db, user, previous=None, workspace_id=None, request=None, action="auth.login"):
    if workspace_id is None:
        workspace_id = db.scalar(select(Membership.workspace_id).where(Membership.user_id == user.id))
    if workspace_id is None:
        raise HTTPException(403, "账号没有可用的工作区")
    if previous:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == digest(previous)))
    settings = get_settings()
    raw = secrets.token_urlsafe(48)
    session = LoginSession(
        token_hash=digest(raw), user_id=user.id, workspace_id=workspace_id,
        csrf_token=secrets.token_urlsafe(32), expires_at=now() + timedelta(hours=settings.auth_session_hours),
    )
    db.add(session)
    record_audit(
        db, workspace_id=workspace_id, actor_user_id=user.id, action=action,
        resource_type="session", request=request,
    )
    db.commit()
    response = JSONResponse(session_info(db, session))
    response.set_cookie(COOKIE, raw, httponly=True, secure=settings.auth_cookie_secure,
                        samesite="lax", max_age=settings.auth_session_hours * 3600, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/options")
def options(db: Session = Depends(get_db)):
    settings = get_settings()
    # The setup link is only useful while a server-issued one-time token is
    # active. Do not advertise a dead public entry point when setup has not
    # been prepared from the server console (or when its token expired).
    workspace_claimed = db.scalar(
        select(Membership).where(Membership.workspace_id == LEGACY_WORKSPACE_ID)
    ) is not None
    bootstrap_ready = db.scalar(
        select(AuthToken).where(
            AuthToken.purpose == "bootstrap",
            AuthToken.expires_at > now(),
        )
    ) is not None
    return {
        "oauth_name": settings.oidc_name if settings.oidc_configured else None,
        "password_reset": settings.mail_configured,
        "setup_available": not workspace_claimed and bootstrap_ready,
    }


@router.post("/register", status_code=201)
def register(payload: Registration, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "register", limit=5)
    if db.scalar(select(User).where(User.email == normalize_email(payload.email))):
        raise HTTPException(409, "邮箱已被使用，请登录或找回密码")
    try:
        user, workspace_id = create_user(db, payload.email, payload.name, payload.password)
        response = login_response(db, user, request.cookies.get(COOKIE), workspace_id, request, "auth.register")
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "邮箱已被使用") from exc
    response.status_code = 201
    return response


@router.post("/login")
def login(payload: Credentials, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "login-ip", limit=60)
    rate_limit(request, "login-account", normalize_email(payload.email))
    user = db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    valid = passwords.verify(payload.password, user.password_hash if user and user.password_hash else DUMMY_HASH)
    if not user or not user.password_hash or not valid:
        raise HTTPException(401, "邮箱或密码不正确")
    return login_response(db, user, request.cookies.get(COOKIE), request=request)


@router.get("/me")
def me(session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    return JSONResponse(session_info(db, session), headers={"Cache-Control": "no-store"})


@router.post("/logout", status_code=204)
def logout(request: Request, session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    record_audit(
        db, workspace_id=session.workspace_id, actor_user_id=session.user_id,
        action="auth.logout", resource_type="session", request=request,
    )
    db.delete(session)
    db.commit()
    response = JSONResponse(None, status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=get_settings().auth_cookie_secure, httponly=True, samesite="lax")
    return response


class PasswordChange(BaseModel):
    current_password: str = Field(default="", max_length=128)
    password: str = Field(min_length=12, max_length=128)


@router.post("/password")
def change_password(payload: PasswordChange, request: Request,
                    session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    rate_limit(request, "password", limit=10)
    user = db.get(User, session.user_id)
    if user.password_hash and not passwords.verify(payload.current_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    user.password_hash = passwords.hash(payload.password)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id, LoginSession.token_hash != session.token_hash))
    record_audit(
        db, workspace_id=session.workspace_id, actor_user_id=session.user_id,
        action="auth.password_changed", resource_type="user", resource_id=user.id, request=request,
    )
    db.commit()
    return {"message": "密码已更新，其他设备已退出登录"}


@router.post("/bootstrap")
def bootstrap(payload: Bootstrap, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "bootstrap", limit=5)
    # Serializing on the reserved workspace prevents two initial owners.
    db.scalar(select(Workspace).where(Workspace.id == LEGACY_WORKSPACE_ID).with_for_update())
    if db.scalar(select(Membership).where(Membership.workspace_id == LEGACY_WORKSPACE_ID)):
        raise HTTPException(409, "工作区已经完成初始化")
    token = db.get(AuthToken, digest(payload.token))
    if token is None or token.purpose != "bootstrap" or aware(token.expires_at) <= now():
        raise HTTPException(400, "初始化口令无效或已过期")
    user = db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    if user:
        if not user.password_hash or not passwords.verify(payload.password, user.password_hash):
            raise HTTPException(401, "邮箱或密码不正确")
    else:
        user, _ = create_user(db, payload.email, payload.name, payload.password)
    db.add(Membership(workspace_id=LEGACY_WORKSPACE_ID, user_id=user.id, role="admin"))
    db.execute(update(Conversation).where(
        Conversation.workspace_id == LEGACY_WORKSPACE_ID, Conversation.owner_id.is_(None),
    ).values(owner_id=user.id))
    db.execute(update(QueryTrace).where(
        QueryTrace.owner_id.is_(None),
        QueryTrace.knowledge_base_id.in_(select(KnowledgeBase.id).where(KnowledgeBase.workspace_id == LEGACY_WORKSPACE_ID)),
    ).values(owner_id=user.id))
    db.execute(delete(AuthToken).where(AuthToken.purpose == "bootstrap"))
    return login_response(db, user, request.cookies.get(COOKIE), LEGACY_WORKSPACE_ID, request, "auth.bootstrap")


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=128)


def send_reset(email, token):
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = "知库 - 重置密码"
    message.set_content(f"请在 30 分钟内打开链接重置密码：\n{settings.frontend_url}/?reset={token}\n"
                        "如果不是你本人操作，请忽略此邮件。")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value() if settings.smtp_password else "")
        smtp.send_message(message)


@router.post("/forgot-password")
def forgot(payload: EmailRequest, request: Request, db: Session = Depends(get_db)):
    if not get_settings().mail_configured:
        raise HTTPException(503, "管理员尚未配置密码找回邮件服务")
    rate_limit(request, "forgot", limit=5)
    user = db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    if user:
        raw = secrets.token_urlsafe(48)
        db.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == "reset"))
        db.add(AuthToken(token_hash=digest(raw), purpose="reset", user_id=user.id, expires_at=now()+timedelta(minutes=30)))
        db.commit()
        try:
            send_reset(user.email, raw)
        except (OSError, smtplib.SMTPException):
            # Same public result for existing/unknown email, including delivery failures.
            db.execute(delete(AuthToken).where(AuthToken.token_hash == digest(raw)))
            db.commit()
    return {"message": "如果该邮箱已注册，将收到密码重置邮件"}


@router.post("/reset-password")
def reset(payload: ResetPassword, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "reset", limit=10)
    new_hash = passwords.hash(payload.password)
    user_id = db.scalar(delete(AuthToken).where(
        AuthToken.token_hash == digest(payload.token), AuthToken.purpose == "reset",
        AuthToken.expires_at > now(),
    ).returning(AuthToken.user_id))
    if user_id is None:
        db.rollback()
        raise HTTPException(400, "重置链接无效或已过期")
    db.execute(update(User).where(User.id == user_id).values(password_hash=new_hash))
    db.execute(delete(AuthToken).where(AuthToken.user_id == user_id, AuthToken.purpose == "reset"))
    db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
    for workspace_id in db.scalars(select(Membership.workspace_id).where(Membership.user_id == user_id)):
        record_audit(
            db, workspace_id=workspace_id, actor_user_id=user_id,
            action="auth.password_reset", resource_type="user", resource_id=user_id, request=request,
        )
    db.commit()
    response = JSONResponse({"message": "密码已更新，请重新登录"})
    response.delete_cookie(COOKIE, path="/")
    return response


class SwitchWorkspace(BaseModel):
    workspace_id: UUID


@router.post("/workspace")
def switch(payload: SwitchWorkspace, request: Request,
           session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    if db.get(Membership, (payload.workspace_id, session.user_id)) is None:
        raise HTTPException(404, "工作区不存在")
    previous_workspace_id = session.workspace_id
    session.workspace_id = payload.workspace_id
    record_audit(
        db, workspace_id=payload.workspace_id, actor_user_id=session.user_id,
        action="auth.workspace_switched", resource_type="workspace",
        resource_id=payload.workspace_id, metadata={"from_workspace_id": str(previous_workspace_id)}, request=request,
    )
    db.commit()
    return session_info(db, session)


@router.get("/members")
def members(access: Access = Depends(get_access), db: Session = Depends(get_db)):
    access.admin()
    return [{"id": str(user.id), "name": user.name, "email": user.email, "role": role}
            for user, role in db.execute(select(User, Membership.role).join(
                Membership, Membership.user_id == User.id,
            ).where(Membership.workspace_id == access.workspace_id)).all()]


class MemberUpdate(BaseModel):
    email: EmailStr
    role: Literal["admin", "editor", "viewer"]


@router.put("/members")
def set_member(payload: MemberUpdate, request: Request, access: Access = Depends(get_access), db: Session = Depends(get_db)):
    access.admin()
    # Lock all membership changes on the same workspace, including last-admin protection.
    db.scalar(select(Workspace).where(Workspace.id == access.workspace_id).with_for_update())
    user = db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    if user is None:
        raise HTTPException(404, "该邮箱尚未注册，请对方先创建账号")
    member = db.get(Membership, (access.workspace_id, user.id))
    if member and member.role == "admin" and payload.role != "admin":
        protect_last_admin(db, access.workspace_id)
    if member:
        member.role = payload.role
        action = "workspace.member_role_changed"
    else:
        db.add(Membership(workspace_id=access.workspace_id, user_id=user.id, role=payload.role))
        action = "workspace.member_added"
    record_audit(
        db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action=action, resource_type="membership", resource_id=user.id,
        metadata={"role": payload.role}, request=request,
    )
    db.commit()
    return {"message": "成员已更新"}


def protect_last_admin(db, workspace_id):
    count = db.scalar(select(func.count()).select_from(Membership).where(
        Membership.workspace_id == workspace_id, Membership.role == "admin",
    ))
    if count <= 1:
        raise HTTPException(409, "工作区至少需要一位管理员")


@router.delete("/members/{user_id}", status_code=204)
def remove_member(user_id: UUID, request: Request, access: Access = Depends(get_access), db: Session = Depends(get_db)):
    access.admin()
    db.scalar(select(Workspace).where(Workspace.id == access.workspace_id).with_for_update())
    member = db.get(Membership, (access.workspace_id, user_id))
    if member is None:
        raise HTTPException(404, "成员不存在")
    if member.role == "admin":
        protect_last_admin(db, access.workspace_id)
    db.execute(delete(LibraryGrant).where(LibraryGrant.user_id == user_id,
        LibraryGrant.knowledge_base_id.in_(select(KnowledgeBase.id).where(KnowledgeBase.workspace_id == access.workspace_id))))
    db.delete(member)
    record_audit(
        db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="workspace.member_removed", resource_type="membership", resource_id=user_id,
        metadata={"role": member.role}, request=request,
    )
    db.commit()


def oidc_client():
    settings = get_settings()
    if not settings.oidc_configured:
        raise HTTPException(503, "尚未配置外部登录")
    oauth = OAuth()
    oauth.register(
        "oidc", server_metadata_url=settings.oidc_metadata_url,
        client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret.get_secret_value(),
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth.create_client("oidc")


class OAuthStart(BaseModel):
    link: bool = False


@router.post("/oauth/start")
async def oauth_start(payload: OAuthStart, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "oauth", limit=20)
    client = oidc_client()
    request.session.clear()
    if payload.link:
        session = current_session(request, db)
        request.session["link_session"] = session.token_hash
    response = await client.authorize_redirect(request, f"{get_settings().api_public_url}/auth/oauth/callback")
    return {"url": response.headers["location"]}


@router.get("/oauth/callback")
async def oauth_callback(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        client = oidc_client()
        token = await client.authorize_access_token(request)
        info = token.get("userinfo")
        if not info or not info.get("sub") or not info.get("iss"):
            raise ValueError("missing verified OIDC identity")
        identity_key = (info["iss"], info["sub"])
        identity = db.get(ExternalIdentity, identity_key)
        linking = request.session.get("link_session")
        if linking:
            current = current_session(request, db)
            if not secrets.compare_digest(linking, current.token_hash):
                raise ValueError("link session changed")
            if identity and identity.user_id != current.user_id:
                raise ValueError("identity already linked")
            if not identity:
                db.add(ExternalIdentity(provider=info["iss"], subject=info["sub"], user_id=current.user_id))
            db.commit()
            return RedirectResponse(f"{settings.frontend_url}/?account=1", status_code=303)
        if identity:
            user = db.get(User, identity.user_id)
        else:
            if info.get("email_verified") is not True or not info.get("email"):
                raise ValueError("verified email is required")
            email = str(EmailRequest(email=info["email"]).email).lower()
            if db.scalar(select(User).where(User.email == email)):
                return RedirectResponse(f"{settings.frontend_url}/?auth_error=link_required", status_code=303)
            user, _ = create_user(db, email, (info.get("name") or email.split("@")[0])[:100])
            db.add(ExternalIdentity(provider=info["iss"], subject=info["sub"], user_id=user.id))
        signed_in = login_response(db, user, request.cookies.get(COOKIE), request=request)
        response = RedirectResponse(settings.frontend_url, status_code=303)
        for cookie in signed_in.headers.getlist("set-cookie"):
            response.headers.append("set-cookie", cookie)
        return response
    except Exception:
        db.rollback()
        return RedirectResponse(f"{settings.frontend_url}/?auth_error=oauth_failed", status_code=303)
    finally:
        request.session.clear()


class LibraryPermissions(BaseModel):
    restricted: bool
    user_ids: list[UUID] = Field(default_factory=list, max_length=1000)


permissions_router = APIRouter(tags=["permissions"])


@permissions_router.get("/knowledge-bases/{id}/permissions")
def library_permissions(id: UUID, access: Access = Depends(get_access), db: Session = Depends(get_db)):
    access.admin()
    library = access.library(db, id)
    return {"restricted": library.restricted, "user_ids": list(db.scalars(
        select(LibraryGrant.user_id).where(LibraryGrant.knowledge_base_id == id)).all())}


@permissions_router.put("/knowledge-bases/{id}/permissions")
def update_library_permissions(id: UUID, payload: LibraryPermissions,
                               request: Request, access: Access = Depends(get_access), db: Session = Depends(get_db)):
    access.admin()
    db.scalar(select(Workspace).where(Workspace.id == access.workspace_id).with_for_update())
    library = access.library(db, id)
    ids = set(payload.user_ids)
    if ids and len(db.scalars(select(Membership.user_id).where(
        Membership.workspace_id == access.workspace_id, Membership.user_id.in_(ids),
    )).all()) != len(ids):
        raise HTTPException(400, "只能授权给当前工作区成员")
    library.restricted = payload.restricted
    db.execute(delete(LibraryGrant).where(LibraryGrant.knowledge_base_id == id))
    db.add_all([LibraryGrant(knowledge_base_id=id, user_id=user_id) for user_id in ids] if payload.restricted else [])
    record_audit(
        db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="knowledge_base.permission_changed", resource_type="knowledge_base", resource_id=id,
        metadata={"restricted": payload.restricted, "member_count": len(ids)}, request=request,
    )
    db.commit()
    return {"message": "访问权限已更新"}
