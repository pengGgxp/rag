"""Browser-bound, short-lived email ownership proofs. No session before verification."""
import hmac
import secrets
from datetime import timedelta

import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .schemas import CompleteRegistration, ConfirmEmail, SendCode, VerifyCode
from app.infra.config import get_settings
from app.infra.db import get_db
from ..models import AuthToken, AuthenticationFlow, ExternalIdentity, LoginSession, User
from .site_auth import configuration, email_enabled, send_code_mail

router = APIRouter(prefix="/auth", tags=["email-authentication"])
FLOW_COOKIE = "rag_auth_flow"
FLOW_SECONDS = 1800


def email_rate_limit(request, db, email, flow, *, sending=True):
    ip = request.client.host if request.client else "unknown"
    keys = [f"email:ip:{auth.digest(ip)}", f"email:address:{auth.digest(email)}", f"email:flow:{auth.digest(flow)}"]
    limits = [60, 15, 15] if sending else [150, 50, 30]
    suffix = "send" if sending else "verify"
    keys = [f"{key}:{suffix}" for key in keys]
    if sending:
        keys.append("email:site:hour")
        limits.append(configuration(db).get("policy", {}).get("hourly_email_limit", 100))
    script = """local allowed=1
for i,key in ipairs(KEYS) do
  local count=tonumber(redis.call('GET',key) or '0')
  if count>=tonumber(ARGV[i]) then allowed=0 end
end
if allowed==0 then return 0 end
for i,key in ipairs(KEYS) do
  local n=redis.call('INCR',key)
  if n==1 then redis.call('EXPIRE',key,3600) end
end
return 1
"""
    try:
        store = redis.Redis.from_url(get_settings().redis_url, socket_timeout=3, socket_connect_timeout=3)
        allowed = store.eval(script, len(keys), *keys, *limits)
    except redis.RedisError as exc:
        raise HTTPException(503, "验证服务暂不可用，请稍后重试") from exc
    if not allowed:
        raise HTTPException(429, "操作过于频繁，请稍后重试")


def safe_return(value):
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    if value.startswith("/auth"):
        return "/"
    return value[:2000]


def flow_cookie(response, raw):
    response.set_cookie(FLOW_COOKIE, raw, httponly=True, secure=get_settings().auth_cookie_secure,
                        samesite="lax", max_age=FLOW_SECONDS, path="/")


def find_flow(request, db, required=True):
    raw = request.cookies.get(FLOW_COOKIE, "")
    row = db.scalar(select(AuthenticationFlow).where(AuthenticationFlow.token_hash == auth.digest(raw)).with_for_update()) if raw else None
    if row is None or auth.aware(row.expires_at) <= auth.now():
        if required:
            raise HTTPException(409, "验证流程已过期，请重新开始")
        return None
    return row


def new_flow(request, db, purpose, *, user=None, email=None, data=None):
    old = find_flow(request, db, required=False)
    if old:
        db.delete(old)
    db.execute(delete(AuthenticationFlow).where(AuthenticationFlow.expires_at < auth.now()))
    db.execute(delete(AuthToken).where(AuthToken.purpose == "wechat_code", AuthToken.expires_at < auth.now()))
    raw = secrets.token_urlsafe(48)
    row = AuthenticationFlow(token_hash=auth.digest(raw), purpose=purpose, user_id=user.id if user else None,
        email=email, data={"stage": "email", **(data or {})}, expires_at=auth.now()+timedelta(seconds=FLOW_SECONDS))
    db.add(row)
    db.flush()
    return row, raw


def flow_view(row):
    return {"purpose": row.purpose, "stage": row.data.get("stage", "email"), "email": row.email or "",
            "expires_at": row.expires_at.isoformat(), "return_to": safe_return(row.data.get("return_to")),
            "retry_after": max(0, 60-int((auth.now()-auth.aware(row.sent_at)).total_seconds())) if row.sent_at else 0}


def pending(row, raw=None):
    response = JSONResponse({"status": "pending", "flow": flow_view(row)})
    if raw:
        flow_cookie(response, raw)
    return response


def password_verification(request, db, user, *, password_proven=False, return_to="/"):
    if db.scalar(select(User.is_suspended).where(User.id == user.id)):
        raise HTTPException(403, "账号已停用，请联系站点管理员")
    row, raw = new_flow(request, db, "verify", user=user, email=user.email,
        data={"password_proven": password_proven, "return_to": safe_return(return_to)})
    db.commit()
    response = pending(row, raw)
    if password_proven:
        response.delete_cookie(auth.COOKIE, path="/", secure=get_settings().auth_cookie_secure, httponly=True, samesite="lax")
    return response


def code_digest(row, code):
    key = get_settings().auth_secret_key or get_settings().site_config_encryption_key
    if not key:
        raise HTTPException(503, "验证服务尚未配置")
    return hmac.new(key.get_secret_value().encode(), f"{row.token_hash}:{row.purpose}:{row.email}:{code}".encode(), "sha256").hexdigest()


@router.get("/flow")
def get_flow(request: Request, db: Session = Depends(get_db)):
    row = find_flow(request, db, required=False)
    return {"flow": flow_view(row) if row else None}


@router.post("/email/send-code")
def send_code(payload: SendCode, request: Request, db: Session = Depends(get_db)):
    if not email_enabled(db):
        raise HTTPException(503, "邮件登录暂未开放，请使用其他登录方式")
    row = find_flow(request, db, required=False)
    raw = None
    if payload.purpose == "wechat":
        if not row or row.purpose != "wechat" or not row.data.get("identity"):
            raise HTTPException(409, "请重新完成微信授权")
    elif payload.purpose == "verify":
        if not row or row.purpose != "verify":
            session = auth.current_session(request, db)
            user = db.get(User, session.user_id)
            row, raw = new_flow(request, db, "verify", user=user, email=user.email,
                data={"session_hash": session.token_hash, "return_to": safe_return(payload.return_to)})
        elif row.data.get("session_hash"):
            session = auth.current_session(request, db)
            if session.token_hash != row.data["session_hash"] or session.user_id != row.user_id:
                raise HTTPException(409, "登录状态已改变，请重新验证")
    elif not row or row.purpose != payload.purpose or row.email != auth.normalize_email(payload.email or ""):
        if not payload.email:
            raise HTTPException(422, "请输入邮箱")
        row, raw = new_flow(request, db, payload.purpose, email=auth.normalize_email(payload.email),
                            data={"return_to": safe_return(payload.return_to)})
    if row.purpose in ("register", "wechat"):
        if not payload.email:
            raise HTTPException(422, "请输入邮箱")
        email = auth.normalize_email(payload.email)
        if db.scalar(select(User.id).where(User.email == email)):
            raise HTTPException(409, "该邮箱已注册，请先登录原账号，再在我的账号中绑定微信" if row.purpose == "wechat" else "该邮箱已注册，请前往登录")
        row.email = email
    if not row.email:
        raise HTTPException(422, "请输入邮箱")
    email_rate_limit(request, db, row.email, row.token_hash)
    if row.sent_at and (auth.now()-auth.aware(row.sent_at)).total_seconds() < 60:
        raise HTTPException(429, "请等待 60 秒后重新发送")
    if row.data.get("stage") == "profile":
        raise HTTPException(409, "邮箱已验证，请完成注册")
    code = f"{secrets.randbelow(1000000):06d}"
    row.code_hash = code_digest(row, code)
    row.attempts = 0
    row.sent_at = auth.now()
    row.code_expires_at = auth.now()+timedelta(minutes=10)
    row.data = {**row.data, "stage": "code"}
    # Unknown email addresses receive the same response, but no usable challenge.
    deliver = row.purpose != "login" or bool(db.scalar(select(User.id).where(User.email == row.email)))
    try:
        if deliver:
            send_code_mail(db, row.email, code, row.purpose)
        else:
            row.code_hash = None
    except HTTPException:
        row.code_hash = None
        row.sent_at = None
        from app.operations.site_audit import record
        record(db, None, "auth.email_delivery_failed", "user", row.user_id, request=request, result="failed")
        db.commit()
        response = JSONResponse({"detail": "邮件发送失败，请稍后重试"}, status_code=503)
        if raw:
            flow_cookie(response, raw)
        return response
    db.commit()
    return pending(row, raw)


def finish_login(request, db, row, user):
    db.delete(row)
    response = auth.login_response(db, user, request.cookies.get(auth.COOKIE), request=request)
    response.delete_cookie(FLOW_COOKIE, path="/")
    return response


@router.post("/email/verify")
def verify_code(payload: VerifyCode, request: Request, db: Session = Depends(get_db)):
    row = find_flow(request, db)
    email_rate_limit(request, db, row.email or "", row.token_hash, sending=False)
    if row.data.get("stage") != "code" or not row.code_hash or not row.code_expires_at or auth.aware(row.code_expires_at) <= auth.now() or row.attempts >= 5:
        from app.operations.site_audit import record
        record(db, None, "auth.email_verification_failed", "user", row.user_id, request=request, result="failed")
        db.commit()
        raise HTTPException(400, "验证码已失效，请重新发送")
    row.attempts += 1
    if not secrets.compare_digest(row.code_hash, code_digest(row, payload.code)):
        if row.attempts >= 5:
            row.code_hash = None
        from app.operations.site_audit import record
        record(db, None, "auth.email_verification_failed", "user", row.user_id, request=request, result="failed")
        db.commit()
        raise HTTPException(400, "验证码不正确")
    row.code_hash = None
    if row.purpose in ("register", "wechat"):
        row.data = {**row.data, "stage": "profile"}
        db.commit()
        return pending(row)
    user = db.scalar(select(User).where(User.id == row.user_id).with_for_update()) if row.user_id else db.scalar(select(User).where(User.email == row.email).with_for_update())
    if not user or user.email != row.email:
        raise HTTPException(409, "账号状态已改变，请重新开始")
    if user.is_suspended:
        raise HTTPException(403, "账号已停用，请联系站点管理员")
    session_hash = row.data.get("session_hash")
    if session_hash:
        current = auth.current_session(request, db)
        if current.token_hash != session_hash or current.user_id != user.id:
            raise HTTPException(409, "登录状态已改变，请重新验证")
    if not user.email_verified_at:
        if not row.data.get("password_proven"):
            user.password_hash = None
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        user.email_verified_at = auth.now()
        from app.operations.site_audit import record
        record(db, user, "auth.email_verified", "user", user.id, request=request)
    return finish_login(request, db, row, user)


@router.post("/email/confirm-link")
def confirm_link(payload: ConfirmEmail, request: Request, db: Session = Depends(get_db)):
    from app.operations.site_audit import record
    auth.rate_limit(request, "confirm-email", limit=15)
    candidate = db.get(AuthToken, auth.digest(payload.token))
    if not candidate or candidate.purpose != "site_verify" or auth.aware(candidate.expires_at) <= auth.now():
        record(db, None, "auth.email_verification_failed", "user", request=request, result="failed")
        db.commit()
        raise HTTPException(400, "验证链接无效或已过期")
    user = db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    if not user or user.is_suspended:
        raise HTTPException(403, "账号不可用，请联系站点管理员")
    consumed = db.execute(delete(AuthToken).where(AuthToken.token_hash == auth.digest(payload.token),
        AuthToken.purpose == "site_verify", AuthToken.expires_at > auth.now()).returning(AuthToken.user_id).execution_options(synchronize_session="fetch")).scalar_one_or_none()
    if consumed is None:
        raise HTTPException(400, "验证链接无效或已过期")
    if not user.email_verified_at:
        user.email_verified_at = auth.now()
        # This proves email ownership, not the old unverified password.
        user.password_hash = None
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    record(db, user, "auth.email_verified", "user", user.id, request=request, details={"source": "site_admin_resend"})
    db.commit()
    return {"message": "邮箱验证成功，请使用邮箱验证码登录后设置密码"}


@router.post("/complete-registration", status_code=201)
def complete_registration(payload: CompleteRegistration, request: Request, db: Session = Depends(get_db)):
    row = find_flow(request, db)
    if row.purpose not in ("register", "wechat") or row.data.get("stage") != "profile" or not row.email:
        raise HTTPException(409, "请先验证邮箱")
    if (row.purpose == "register" or payload.password) and len(payload.password or "") < 12:
        raise HTTPException(422, "密码至少需要 12 个字符")
    try:
        user = auth.create_user(db, row.email, payload.name, payload.password)
        user.email_verified_at = auth.now()
        identity = row.data.get("identity")
        if row.purpose == "wechat":
            if not identity:
                raise HTTPException(409, "微信授权已失效")
            db.add(ExternalIdentity(provider=identity["provider"], subject=identity["subject"], user_id=user.id))
        response = finish_login(request, db, row, user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "邮箱或微信已被使用，请重新登录") from exc
    response.status_code = 201
    return response
