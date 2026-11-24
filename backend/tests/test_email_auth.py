"""Email and custom WeChat flows exercise real challenge/session handling."""
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
import redis
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

from app.identity import auth, email_auth, site_auth, wechat_auth
from app.models import AuthenticationFlow, CreditLedgerEntry, ExternalIdentity, LoginSession, SiteAuthAudit, SiteAuthSettings, User
from test_auth import PASSWORD, register


@pytest.fixture
def delivery(accounts, monkeypatch):
    clients, db = accounts
    settings = auth.get_settings().model_copy(update={"auth_secret_key": SecretStr("test-auth-secret-with-at-least-32-characters"),
        "site_config_encryption_key": SecretStr(Fernet.generate_key().decode()), "auth_cookie_secure": False})
    for module in (auth, email_auth, site_auth, wechat_auth):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    counts = {}
    class Store:
        def eval(self, _script, count, *args):
            keys, limits = args[:count], args[count:]
            if any(counts.get(key, 0) >= int(limit) for key, limit in zip(keys, limits)):
                return 0
            for key in keys:
                counts[key] = counts.get(key, 0) + 1
            return 1
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: Store())
    config = SiteAuthSettings(id=1, config={"smtp": {"enabled": True, "host": "smtp.example.com", "port": 587,
        "tls_mode": "starttls", "username": "mailer", "secret": site_auth.protect("smtp-password"),
        "from_address": "mail@example.com", "from_name": "知库"},
        "policy": {"require_verified_email": True, "hourly_email_limit": 100}})
    db.add(config); db.commit()
    sent = []
    monkeypatch.setattr(email_auth, "send_code_mail", lambda db, email, code, purpose="verify": sent.append((email, code)))
    return clients, db, sent, counts


def send(client, email="new@example.com", purpose="register"):
    result = client.post("/auth/email/send-code", json={"email": email, "purpose": purpose})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "pending"
    return result


def verify(client, sent):
    result = client.post("/auth/email/verify", json={"code": sent[-1][1]})
    assert result.status_code == 200, result.text
    return result


def legacy(db, email="legacy@example.com", verified=False):
    user = auth.create_user(db, email, "Legacy", PASSWORD)
    if verified: user.email_verified_at = auth.now()
    db.commit()
    return user


def flow(client, db):
    return db.get(AuthenticationFlow, auth.digest(client.cookies.get(email_auth.FLOW_COOKIE)))


def test_registration_does_not_create_anything_until_verified_and_completed(delivery):
    (client, other, _), db, sent, _ = delivery
    send(client)
    assert client.get("/knowledge-bases").status_code == 401
    assert db.scalar(select(func.count()).select_from(User)) == 0
    assert client.get("/auth/flow").json()["flow"]["email"] == "new@example.com"
    assert other.post("/auth/email/verify", json={"code": sent[-1][1]}).status_code == 409
    verified = verify(client, sent)
    assert verified.json()["flow"]["stage"] == "profile"
    assert db.scalar(select(func.count()).select_from(User)) == 0
    result = client.post("/auth/complete-registration", json={"name": "New", "password": PASSWORD})
    assert result.status_code == 201, result.text
    assert result.json()["user"]["email_verified"] is True
    assert result.json()["user"]["is_site_admin"] is False
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry)) == 1
    assert client.post("/auth/complete-registration", json={"name": "Again", "password": PASSWORD}).status_code == 409
    assert client.post("/auth/register", json={"name": "Bypass", "email": "bypass@example.com", "password": PASSWORD}).status_code == 409
    assert db.scalar(select(func.count()).select_from(User)) == 1


def test_old_password_login_requires_email_but_preserves_proven_password(delivery):
    (client, _, _), db, sent, _ = delivery
    user = legacy(db)
    result = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    assert result.json()["status"] == "pending"
    assert client.get("/auth/me").status_code == 401
    send(client, user.email, "verify")
    session = verify(client, sent).json()
    assert session["user"]["email_verified"] and session["user"]["has_password"]
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).json().get("user")


def test_email_first_proof_revokes_legacy_sessions_and_password(delivery):
    (client, other, _), db, sent, _ = delivery
    user = legacy(db)
    signed = auth.login_response(db, user)
    old_sessions = db.scalars(select(LoginSession)).all()
    assert len(old_sessions) == 1
    old_hash = old_sessions[0].token_hash
    send(client, user.email, "login")
    result = verify(client, sent).json()
    assert result["user"]["has_password"] is False
    assert db.get(LoginSession, old_hash) is None
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 401
    client.headers["X-CSRF-Token"] = result["csrf_token"]
    assert client.post("/auth/password", json={"password": PASSWORD}).status_code == 200


def test_verified_code_login_keeps_existing_password(delivery):
    (client, _, _), db, sent, _ = delivery
    user = legacy(db, verified=True)
    send(client, user.email, "login")
    assert verify(client, sent).json()["user"]["has_password"]
    assert client.post("/auth/email/verify", json={"code": sent[-1][1]}).status_code == 409


def test_unknown_code_login_never_registers(delivery):
    (client, _, _), db, sent, _ = delivery
    send(client, "unknown@example.com", "login")
    assert sent == []
    assert client.post("/auth/email/verify", json={"code": "000000"}).status_code == 400
    assert db.scalar(select(func.count()).select_from(User)) == 0


def test_resend_expiry_attempts_and_purpose_isolation(delivery):
    (client, _, _), db, sent, _ = delivery
    send(client)
    old_code = sent[-1][1]
    assert client.post("/auth/email/send-code", json={"email": "new@example.com", "purpose": "register"}).status_code == 429
    row = flow(client, db)
    row.sent_at -= timedelta(seconds=61); db.commit()
    send(client)
    assert flow(client, db).code_hash == email_auth.code_digest(flow(client, db), sent[-1][1])
    if old_code != sent[-1][1]:
        assert client.post("/auth/email/verify", json={"code": old_code}).status_code == 400
    row = flow(client, db); row.code_expires_at = auth.now()-timedelta(seconds=1); db.commit()
    assert client.post("/auth/email/verify", json={"code": sent[-1][1]}).status_code == 400
    row.sent_at -= timedelta(seconds=61); db.commit(); send(client)
    wrong = "111111" if sent[-1][1] != "111111" else "222222"
    for _ in range(5):
        assert client.post("/auth/email/verify", json={"code": wrong}).status_code == 400
    assert client.post("/auth/email/verify", json={"code": sent[-1][1]}).status_code == 400
    row.sent_at -= timedelta(seconds=61); db.commit()
    old_code = sent[-1][1]
    send(client, "new@example.com", "login")
    assert client.post("/auth/email/verify", json={"code": old_code}).status_code == 400


def test_failed_smtp_invalidates_challenge_and_redis_fails_closed(delivery, monkeypatch):
    (client, _, _), db, sent, _ = delivery
    from fastapi import HTTPException
    def fail(*_a): raise HTTPException(503, "SMTP failed")
    monkeypatch.setattr(email_auth, "send_code_mail", fail)
    assert client.post("/auth/email/send-code", json={"email": "new@example.com", "purpose": "register"}).status_code == 503
    assert flow(client, db).code_hash is None
    def offline(*_a, **_k): raise redis.ConnectionError()
    monkeypatch.setattr(redis.Redis, "from_url", offline)
    assert client.post("/auth/email/send-code", json={"email": "another@example.com", "purpose": "register"}).status_code == 503


def test_site_admin_is_independent_and_secrets_are_encrypted(delivery):
    (client, _, _), db, _, _ = delivery
    user = legacy(db, verified=True)
    result = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).json()
    client.headers["X-CSRF-Token"] = result["csrf_token"]
    assert client.get("/site/settings/auth").status_code == 403
    assert client.put("/site/settings/auth", json={"policy": {}}).status_code == 403
    user.is_site_admin = True; db.commit()
    smtp = site_auth.public_config(db)["smtp"]
    smtp.pop("has_password")
    smtp["password"] = "unique-sensitive-password"
    response = client.put("/site/settings/auth", json={"smtp": smtp})
    assert response.status_code == 200, response.text
    assert "unique-sensitive-password" not in response.text
    assert "unique-sensitive-password" not in str(db.get(SiteAuthSettings, 1).config)
    assert "unique-sensitive-password" not in str(db.scalars(select(SiteAuthAudit)).all())
    old_secret = db.get(SiteAuthSettings, 1).config["smtp"]["secret"]
    smtp.pop("password")
    assert client.put("/site/settings/auth", json={"smtp": smtp}).status_code == 200
    assert db.get(SiteAuthSettings, 1).config["smtp"]["secret"] == old_secret
    user.is_site_admin = False; db.commit()
    assert client.get("/site/settings/auth").status_code == 403


def wechat_setup(db, monkeypatch, uid="wechat-uid"):
    row = db.get(SiteAuthSettings, 1)
    row.config = {**row.config, "wechat": {"enabled": True, "app_id": "app1", "secret": site_auth.protect("app-secret")}}
    db.commit()
    calls = []
    def provider(config, action, **params):
        calls.append((action, params))
        if action == "login": return {"code": 0, "type": "wx", "url": "https://u.arsn.cn/authorize"}
        return {"code": 0, "type": "wx", "social_uid": uid, "nickname": "untrusted"}
    monkeypatch.setattr(wechat_auth, "provider_request", provider)
    return calls


def start_wechat(client, calls, link=False):
    result = client.post("/auth/wechat/start", json={"link": link, "return_to": "/settings" if link else "/?chat=1"})
    assert result.status_code == 200, result.text
    return urlsplit(calls[-1][1]["redirect_uri"]).path


def test_wechat_first_login_requires_email_and_never_merges(delivery, monkeypatch):
    (client, _, _), db, sent, _ = delivery
    calls = wechat_setup(db, monkeypatch)
    path = start_wechat(client, calls)
    callback = client.get(path, params={"type": "wx", "code": "one-time"}, follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"].endswith("/auth/verify-email")
    assert client.get("/auth/me").status_code == 401
    assert db.scalar(select(func.count()).select_from(User)) == 0
    send(client, "wechat@example.com", "wechat"); verify(client, sent)
    result = client.post("/auth/complete-registration", json={"name": "WeChat"})
    assert result.status_code == 201, result.text
    assert result.json()["user"]["wechat_linked"]
    assert not result.json()["user"]["has_password"]
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry)) == 1
    path = start_wechat(client, calls)
    assert client.get(path, params={"type": "wx", "code": "another"}, follow_redirects=False).status_code == 303
    assert client.get("/auth/me").status_code == 200
    assert db.scalar(select(func.count()).select_from(User)) == 1


def test_wechat_rejects_cross_browser_replay_and_invalid_uid(delivery, monkeypatch):
    (client, other, _), db, _, _ = delivery
    calls = wechat_setup(db, monkeypatch, uid="")
    path = start_wechat(client, calls)
    other.get(path, params={"type": "wx", "code": "forged"}, follow_redirects=False)
    assert len(calls) == 1  # No code exchange before browser ownership proof.
    client.get(path, params={"type": "wx", "code": "invalid-uid"}, follow_redirects=False)
    assert len(calls) == 2
    client.get(path, params={"type": "wx", "code": "replay"}, follow_redirects=False)
    assert len(calls) == 2
    assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 0


def test_wechat_explicit_binding_checks_email_session_and_unlink(delivery, monkeypatch):
    (client, _, _), db, _, _ = delivery
    user = legacy(db, verified=True)
    result = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).json()
    client.headers["X-CSRF-Token"] = result["csrf_token"]
    calls = wechat_setup(db, monkeypatch)
    path = start_wechat(client, calls, link=True)
    assert client.get(path, params={"type": "wx", "code": "bind"}, follow_redirects=False).status_code == 303
    assert client.get("/auth/me").json()["user"]["wechat_linked"]
    assert client.post("/auth/wechat/unlink", json={}).status_code == 200
    assert not client.get("/auth/me").json()["user"]["wechat_linked"]
    path = start_wechat(client, calls, link=True)
    client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    count = len(calls)
    client.get(path, params={"type": "wx", "code": "session-changed"}, follow_redirects=False)
    assert len(calls) == count
    assert not client.get("/auth/me").json()["user"]["wechat_linked"]


def test_wechat_existing_email_requires_explicit_account_link(delivery, monkeypatch):
    (client, _, _), db, _, _ = delivery
    user = legacy(db, verified=True)
    calls = wechat_setup(db, monkeypatch)
    path = start_wechat(client, calls)
    client.get(path, params={"type": "wx", "code": "email-conflict"}, follow_redirects=False)
    response = client.post("/auth/email/send-code", json={"email": user.email, "purpose": "wechat"})
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 0


def test_wechat_code_cannot_be_reused_in_another_browser_flow(delivery, monkeypatch):
    (client, other, _), db, _, _ = delivery
    calls = wechat_setup(db, monkeypatch)
    first = start_wechat(client, calls)
    client.get(first, params={"type": "wx", "code": "repeated-provider-code"}, follow_redirects=False)
    second = start_wechat(other, calls)
    count = len(calls)
    other.get(second, params={"type": "wx", "code": "repeated-provider-code"}, follow_redirects=False)
    assert len(calls) == count
    assert other.get("/auth/flow").json()["flow"]["stage"] == "failed"


def test_expired_wechat_state_and_wrong_type_never_exchange(delivery, monkeypatch):
    (client, _, _), db, _, _ = delivery
    calls = wechat_setup(db, monkeypatch)
    path = start_wechat(client, calls)
    row = flow(client, db)
    row.data = {**row.data, "state_expires_at": (auth.now()-timedelta(seconds=1)).isoformat()}; db.commit()
    client.get(path, params={"type": "wx", "code": "expired"}, follow_redirects=False)
    assert len(calls) == 1
    path = start_wechat(client, calls)
    client.get(path, params={"type": "qq", "code": "wrong-channel"}, follow_redirects=False)
    assert len(calls) == 2


@pytest.mark.parametrize("result", [{"code": 2, "type": "wx"}, {"code": 0, "type": "qq"}, {"code": False, "type": "wx"}, []])
def test_provider_response_validation_and_secret_sanitization(delivery, monkeypatch, result):
    import httpx
    from fastapi import HTTPException
    class Client:
        def __init__(self, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *_a): pass
        def get(self, url, params):
            return httpx.Response(200, json=result, request=httpx.Request("GET", url))
    monkeypatch.setattr(wechat_auth.httpx, "Client", Client)
    with pytest.raises(HTTPException) as exc:
        wechat_auth.provider_request({"app_id": "app1", "secret": site_auth.protect("private-app-key")}, "callback", code="private-code")
    assert "private-app-key" not in str(exc.value) and "private-code" not in str(exc.value)


def test_global_email_cap_and_verification_redis_outage(delivery, monkeypatch):
    (client, other, _), db, sent, counts = delivery
    row = db.get(SiteAuthSettings, 1)
    row.config = {**row.config, "policy": {"require_verified_email": True, "hourly_email_limit": 1}}; db.commit()
    send(client)
    assert other.post("/auth/email/send-code", json={"email": "another@example.com", "purpose": "register"}).status_code == 429
    def offline(*_a, **_k): raise redis.ConnectionError()
    monkeypatch.setattr(redis.Redis, "from_url", offline)
    assert client.post("/auth/email/verify", json={"code": sent[-1][1]}).status_code == 503
    assert flow(client, db).code_hash is not None


@pytest.mark.parametrize("mode,port", [("ssl", 465), ("starttls", 587)])
def test_smtp_tls_and_multipart_mail(delivery, monkeypatch, mode, port):
    (client, _, _), db, _, _ = delivery
    row = db.get(SiteAuthSettings, 1)
    row.config = {**row.config, "smtp": {**row.config["smtp"], "tls_mode": mode, "port": port}}; db.commit()
    events = []
    class SMTP:
        def __init__(self, host, actual_port, **kwargs):
            assert actual_port == port and kwargs["timeout"] == 15
            if mode == "ssl": assert kwargs["context"].check_hostname
        def __enter__(self): return self
        def __exit__(self, *_a): pass
        def starttls(self, context):
            assert context.check_hostname; events.append("starttls")
        def login(self, username, password):
            assert username == "mailer" and password == "smtp-password"; events.append("login")
        def send_message(self, message):
            assert message.get_body(preferencelist=("plain",)) is not None
            assert message.get_body(preferencelist=("html",)) is not None
            assert "mail@example.com" in str(message["From"])
            events.append("send")
    monkeypatch.setattr(site_auth.smtplib, "SMTP_SSL" if mode == "ssl" else "SMTP", SMTP)
    site_auth.send_code_mail(db, "target@example.com", "345678", "register")
    assert events == (["starttls", "login", "send"] if mode == "starttls" else ["login", "send"])


def test_saved_disabled_mail_never_falls_back_to_environment(delivery, monkeypatch):
    (_, _, _), db, _, _ = delivery
    row = db.get(SiteAuthSettings, 1)
    row.config = {**row.config, "smtp": {**row.config["smtp"], "enabled": False}}; db.commit()
    env = auth.get_settings().model_copy(update={"smtp_host": "old.example.com", "smtp_from": "old@example.com"})
    monkeypatch.setattr(site_auth, "get_settings", lambda: env)
    assert site_auth.email_enabled(db) is False
