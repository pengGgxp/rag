"""Senyu's custom wx protocol. Provider credentials never reach the browser."""
import secrets
from datetime import timedelta, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .email_auth import FLOW_COOKIE, find_flow, flow_cookie, new_flow, password_verification, safe_return
from .schemas import WechatStart
from app.infra.config import get_settings
from app.infra.db import get_db
from ..models import AuthToken, ExternalIdentity, User
from .site_auth import email_enabled, reveal, wechat_configuration, wechat_enabled

router = APIRouter(prefix="/auth/wechat", tags=["wechat-authentication"])
PROVIDER_URL = "https://u.arsn.cn/connect.php"


def provider_request(config, action, **params):
    try:
        with httpx.Client(timeout=12, follow_redirects=False) as client:
            result = client.get(PROVIDER_URL, params={"act": action, "appid": config["app_id"],
                "appkey": reveal(config["secret"]), "type": "wx", **params})
            result.raise_for_status()
            data = result.json()
    except (httpx.HTTPError, ValueError, KeyError):
        # Never include the provider request URL: it contains the AppKey.
        raise HTTPException(502, "微信授权服务暂不可用，请重新尝试") from None
    if not isinstance(data, dict) or type(data.get("code")) is not int or data.get("code") != 0 or data.get("type") != "wx":
        raise HTTPException(502, "微信授权未完成，请重新尝试")
    return data


@router.post("/start")
def start(payload: WechatStart, request: Request, db: Session = Depends(get_db)):
    auth.rate_limit(request, "wechat", limit=20)
    if not wechat_enabled(db) or not email_enabled(db):
        raise HTTPException(503, "微信登录暂未开放")
    config = wechat_configuration(db)
    data = {"stage": "authorizing", "return_to": safe_return(payload.return_to), "app_id": config["app_id"],
            "state_expires_at": (auth.now()+timedelta(minutes=10)).isoformat()}
    user = None
    if payload.link:
        session = auth.current_session(request, db)
        user = db.get(User, session.user_id)
        if not user.email_verified_at:
            raise HTTPException(409, "请先验证邮箱，再绑定微信")
        data["link_session"] = session.token_hash
    state = secrets.token_urlsafe(32)
    data["state_hash"] = auth.digest(state)
    row, raw = new_flow(request, db, "wechat", user=user, data=data)
    callback = f"{get_settings().api_public_url.rstrip('/')}/auth/wechat/callback/{state}"
    result = provider_request(config, "login", redirect_uri=callback)
    url = result.get("url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    # The documented url can point directly to the upstream social provider.
    if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(502, "授权地址无效，请重试")
    from fastapi.responses import JSONResponse
    response = JSONResponse({"url": url})
    flow_cookie(response, raw)
    db.commit()
    return response


def redirect(path, source=None):
    response = RedirectResponse(f"{get_settings().frontend_url.rstrip('/')}{path}", status_code=303)
    if source:
        for cookie in source.headers.getlist("set-cookie"):
            response.headers.append("set-cookie", cookie)
    return response


@router.get("/callback/{state}")
def callback(state: str, request: Request, db: Session = Depends(get_db)):
    row = find_flow(request, db, required=False)
    if not row or row.purpose != "wechat" or not row.data.get("state_hash") or not row.data.get("state_expires_at") or auth.aware(datetime.fromisoformat(row.data["state_expires_at"])) <= auth.now() or not secrets.compare_digest(row.data["state_hash"], auth.digest(state)):
        return redirect("/auth/login?error=wechat_expired")
    data = dict(row.data)
    row.data = {**data, "state_hash": None, "stage": "failed"}
    try:
        config = wechat_configuration(db)
        if not wechat_enabled(db) or config["app_id"] != data.get("app_id"):
            raise HTTPException(409, "授权配置已更改")
        code = request.query_params.get("code", "")
        if request.query_params.get("type") != "wx" or not code or len(code) > 2048:
            raise HTTPException(400, "授权已取消")
        # Check browser and originating session before exchanging a one-use code.
        current = None
        if data.get("link_session"):
            current = auth.current_session(request, db)
            if current.token_hash != data["link_session"] or current.user_id != row.user_id:
                raise HTTPException(409, "绑定会话已改变")
            if not db.get(User, current.user_id, populate_existing=True).email_verified_at:
                raise HTTPException(409, "请先验证邮箱")
        db.add(AuthToken(token_hash=auth.digest(f"senyu-code:{config['app_id']}:{code}"), purpose="wechat_code",
                         expires_at=auth.now()+timedelta(minutes=30)))
        # Persist state consumption before the network call, even if this process dies.
        db.commit()
        result = provider_request(config, "callback", code=code)
        row = find_flow(request, db)
        if current:
            fresh = auth.current_session(request, db)
            if fresh.token_hash != data["link_session"] or fresh.user_id != row.user_id:
                raise HTTPException(409, "绑定会话已改变")
        uid = result.get("social_uid")
        if not isinstance(uid, str) or not uid.strip() or len(uid) > 255:
            raise HTTPException(502, "微信身份无效")
        provider = f"senyu:u.arsn.cn:{config['app_id']}:wx"
        identity = db.get(ExternalIdentity, (provider, uid))
        if current:
            db.scalar(select(User).where(User.id == current.user_id).with_for_update())
            if identity and identity.user_id != current.user_id:
                raise HTTPException(409, "微信已被其他账号绑定")
            if db.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == current.user_id, ExternalIdentity.provider == provider, ExternalIdentity.subject != uid)):
                raise HTTPException(409, "请先解除当前微信绑定")
            if not identity:
                db.add(ExternalIdentity(provider=provider, subject=uid, user_id=current.user_id))
            db.delete(row)
            from app.operations.site_audit import record
            record(db, db.get(User, current.user_id), "auth.wechat_linked", "user", current.user_id, request=request)
            db.commit()
            response = redirect(safe_return(data.get("return_to")))
            response.delete_cookie(FLOW_COOKIE, path="/")
            return response
        if identity:
            user = db.get(User, identity.user_id)
            if not user.email_verified_at:
                proof = password_verification(request, db, user, return_to=data.get("return_to", "/"))
                return redirect("/auth/verify-email", proof)
            db.delete(row)
            signed = auth.login_response(db, user, request.cookies.get(auth.COOKIE), request=request)
            response = redirect(safe_return(data.get("return_to")), signed)
            response.delete_cookie(FLOW_COOKIE, path="/")
            return response
        row.data = {"stage": "email", "identity": {"provider": provider, "subject": uid},
                    "return_to": safe_return(data.get("return_to"))}
        db.commit()
        return redirect("/auth/verify-email")
    except (HTTPException, IntegrityError):
        # Consume the callback even on failure; retry always starts a new authorization.
        db.rollback()
        from app.operations.site_audit import record
        record(db, None, "auth.wechat_failed", "user", request=request, result="failed")
        row = find_flow(request, db, required=False)
        if row:
            row.data = {"stage": "failed", "return_to": safe_return(data.get("return_to"))}
        db.commit()
        return redirect("/auth/login?error=wechat_failed")


@router.post("/unlink")
def unlink(request: Request, db: Session = Depends(get_db), session=Depends(auth.current_session)):
    user = db.get(User, session.user_id, populate_existing=True)
    if not user.email_verified_at or not (user.password_hash or email_enabled(db)):
        raise HTTPException(409, "请先验证邮箱并保留可用的邮箱登录方式")
    rows = db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id, ExternalIdentity.provider.like("senyu:%")).with_for_update()).all()
    for row in rows:
        db.delete(row)
    from app.operations.site_audit import record
    record(db, user, "auth.wechat_unlinked", "user", user.id, request=request)
    auth.record_account_audit(db, user.id, "auth.wechat_unlinked", "user", resource_id=user.id, request=request)
    db.commit()
    return {"message": "已解除微信绑定"}
