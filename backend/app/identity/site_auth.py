"""Site-wide authentication configuration; independent of workspace roles."""
import ssl
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.config import get_settings
from app.infra.db import get_db
from ..models import LoginSession, SiteAuthAudit, SiteAuthSettings, User
from .auth import current_session
from .schemas import ConfigInput, TestEmail

router = APIRouter(prefix="/site/settings/auth", tags=["site-authentication"])


def cipher():
    key = get_settings().site_config_encryption_key
    if not key:
        raise HTTPException(503, "请在服务器配置 SITE_CONFIG_ENCRYPTION_KEY")
    try:
        return Fernet(key.get_secret_value().encode())
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "站点配置加密密钥格式不正确") from exc


def protect(value):
    return cipher().encrypt(value.encode()).decode()


def reveal(value):
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "站点配置无法解密，请恢复原加密密钥") from exc


def configuration(db):
    row = db.get(SiteAuthSettings, 1, populate_existing=True)
    return dict(row.config) if row else {}


def smtp_configuration(db):
    config = configuration(db)
    if "smtp" in config:
        return config["smtp"]
    old = get_settings()
    return dict(enabled=old.mail_configured, host=old.smtp_host or "", port=old.smtp_port,
                username=old.smtp_username or "", from_address=old.smtp_from or "", from_name="知库",
                tls_mode="ssl" if old.smtp_port == 465 else "starttls" if old.smtp_starttls else "plain", legacy=True)


def email_enabled(db):
    config = smtp_configuration(db)
    return bool(config.get("enabled") and config.get("host") and config.get("from_address"))


def verification_required(db):
    return configuration(db).get("policy", {}).get("require_verified_email", False)


def wechat_configuration(db):
    return configuration(db).get("wechat", {})


def wechat_enabled(db):
    config = wechat_configuration(db)
    return bool(config.get("enabled") and config.get("app_id") and config.get("secret"))


def send_mail(db, email, subject, text, html):
    config = smtp_configuration(db)
    if not email_enabled(db):
        raise HTTPException(503, "邮件服务暂不可用，请联系站点管理员")
    message = EmailMessage()
    message["From"] = formataddr((config.get("from_name") or "知库", config["from_address"]))
    message["To"] = email
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    password = (get_settings().smtp_password.get_secret_value() if get_settings().smtp_password else "") \
        if config.get("legacy") else reveal(config.get("secret"))
    try:
        context = ssl.create_default_context()
        if config["tls_mode"] == "ssl":
            connection = smtplib.SMTP_SSL(config["host"], config["port"], timeout=15, context=context)
        else:
            connection = smtplib.SMTP(config["host"], config["port"], timeout=15)
        with connection as smtp:
            if config["tls_mode"] == "starttls":
                smtp.starttls(context=context)
            if config.get("username"):
                smtp.login(config["username"], password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(503, "邮件发送失败，请稍后重试") from exc


def send_code_mail(db, email, code, purpose="verify"):
    title = {"register": "验证邮箱并注册", "login": "登录知库", "verify": "验证邮箱", "wechat": "完成微信登录"}.get(purpose, "验证邮箱")
    text = f"你正在{title}。你的知库邮箱验证码是 {code}，10 分钟内有效。请勿向他人透露。\n如果不是你本人操作，请忽略此邮件。"
    send_mail(db, email, f"知库 - {title}", text,
              f"<h1>知库 · {title}</h1><p>你的邮箱验证码</p><p style='font-size:28px'>{code}</p>"
              "<p>10 分钟内有效，请勿向他人透露。</p><p>如果不是你本人操作，请忽略此邮件。</p>")


def site_admin(session: LoginSession = Depends(current_session), db: Session = Depends(get_db)):
    user = db.get(User, session.user_id, populate_existing=True)
    if not user or not user.is_site_admin:
        raise HTTPException(403, "仅站点管理员可以访问站点管理")
    return user


def audit(db, actor, action, details):
    db.add(SiteAuthAudit(actor_user_id=actor.id, action=action, details=details))
    from app.operations.site_audit import record
    record(db, actor, action, "auth_settings", result="failed" if details.get("accepted") is False else "success", details=details)


def public_config(db):
    config = configuration(db)
    smtp = dict(smtp_configuration(db))
    smtp.pop("secret", None)
    smtp["has_password"] = bool(config.get("smtp", {}).get("secret")) if "smtp" in config else bool(get_settings().smtp_password)
    wechat = wechat_configuration(db)
    return {"smtp": smtp, "wechat": {"enabled": wechat.get("enabled", False), "app_id": wechat.get("app_id", ""),
            "has_app_key": bool(wechat.get("secret")),
            "callback_url": f"{get_settings().api_public_url.rstrip('/')}/auth/wechat/callback/<state>"},
            "policy": config.get("policy", {"require_verified_email": False, "hourly_email_limit": 100}),
            "encryption_ready": bool(get_settings().site_config_encryption_key)}


@router.get("")
def read_config(db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    return public_config(db)


@router.put("")
def save_config(payload: ConfigInput, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    # A fixed advisory lock also serializes the initial singleton insert.
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(3020001)"))
    config = configuration(db)
    if payload.smtp is not None:
        value = payload.smtp.model_dump(exclude={"password", "clear_password"})
        if value["enabled"]:
            if not value["host"] or not value["from_address"]:
                raise HTTPException(422, "请填写 SMTP 主机和发件地址")
            try:
                value["from_address"] = str(TypeAdapter(EmailStr).validate_python(value["from_address"]))
            except ValidationError as exc:
                raise HTTPException(422, "发件邮箱格式不正确") from exc
            if get_settings().auth_cookie_secure and value["tls_mode"] == "plain":
                raise HTTPException(422, "生产邮件必须使用 SSL 或 STARTTLS")
        old_secret = config.get("smtp", {}).get("secret")
        if "smtp" not in config and get_settings().smtp_password:
            old_secret = protect(get_settings().smtp_password.get_secret_value())
        value["secret"] = None if payload.smtp.clear_password else old_secret
        if payload.smtp.password and payload.smtp.password.get_secret_value():
            value["secret"] = protect(payload.smtp.password.get_secret_value())
        if value["enabled"] and value["username"] and not value.get("secret"):
            raise HTTPException(422, "配置了 SMTP 账号时必须提供对应密码")
        config["smtp"] = value
    if payload.wechat is not None:
        value = payload.wechat.model_dump(exclude={"app_key", "clear_app_key"})
        old = config.get("wechat", {})
        value["secret"] = None if payload.wechat.clear_app_key else old.get("secret")
        if payload.wechat.app_key and payload.wechat.app_key.get_secret_value():
            value["secret"] = protect(payload.wechat.app_key.get_secret_value())
        if value["enabled"] and (not value["app_id"] or not value.get("secret")):
            raise HTTPException(422, "请填写森遇应用 ID 和 AppKey")
        if old.get("app_id") != value["app_id"] and old.get("secret") and not payload.wechat.app_key:
            raise HTTPException(422, "更换应用 ID 时必须同时填写新的 AppKey")
        config["wechat"] = value
    if payload.policy is not None:
        if payload.policy.require_verified_email and not actor.email_verified_at:
            raise HTTPException(409, "请先在我的账号中验证管理员邮箱，再开启强制验证")
        config["policy"] = payload.policy.model_dump()
    smtp = config.get("smtp", smtp_configuration(db))
    if config.get("policy", {}).get("require_verified_email") and not (smtp.get("enabled") and smtp.get("host") and smtp.get("from_address")):
        raise HTTPException(409, "强制验证需要保持邮件服务启用")
    row = db.get(SiteAuthSettings, 1)
    if row is None:
        row = SiteAuthSettings(id=1)
        db.add(row)
    row.config = config
    audit(db, actor, "auth.settings_updated", {"fields": [f"{section}.{field}" for section in payload.model_fields_set for field in getattr(payload, section).model_fields_set]})
    db.commit()
    return public_config(db)


@router.post("/test-email")
def test_email(payload: TestEmail, request: Request, db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    from .email_auth import email_rate_limit
    email_rate_limit(request, db, str(payload.email), "test")
    try:
        send_mail(db, str(payload.email), "知库 - 邮件服务测试", "这是一封知库测试邮件，收到后即可确认投递。",
                  "<h1>知库邮件服务测试</h1><p>收到这封邮件后，即可确认本次投递成功。</p>")
    except HTTPException:
        audit(db, actor, "auth.email_test", {"accepted": False})
        db.commit()
        raise
    audit(db, actor, "auth.email_test", {"accepted": True})
    db.commit()
    return {"message": "SMTP 服务已接受邮件，请检查收件箱及垃圾邮件"}


@router.get("/audit")
def audit_history(db: Session = Depends(get_db), actor: User = Depends(site_admin)):
    return [{"id": str(row.id), "action": row.action, "actor_user_id": str(row.actor_user_id),
             "details": row.details, "created_at": row.created_at.isoformat()}
            for row in db.scalars(select(SiteAuthAudit).order_by(SiteAuthAudit.created_at.desc()).limit(50))]
