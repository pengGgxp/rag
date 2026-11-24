from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.identity import auth, site_admin
from app.models import (
    AuthToken, ExternalIdentity, LoginSession, SiteAuditEvent, SiteAuthSettings,
    SiteModelProvider, User, UserCreditAccount, UserModelPolicy,
)
from app.site_audit import prune, record
from test_auth import PASSWORD, register
from auth_helpers import connection_id


def setup(accounts):
    (admin, member, anonymous), db = accounts
    a = register(admin)
    b = register(member, "member@example.com", "Member")
    actor = db.get(User, UUID(a["user"]["id"]))
    actor.is_site_admin = True
    db.commit()
    return admin, member, anonymous, db, a, b


def test_all_management_routes_require_fresh_site_role(accounts):
    admin, member, anonymous, db, a, b = setup(accounts)
    user = b["user"]["id"]
    for method, path, body in [
        ("GET", "/overview", None), ("GET", "/users", None), ("GET", f"/users/{user}", None),
        ("GET", "/audit", None),
        ("PUT", f"/users/{user}/status", {"suspended": True}),
        ("PUT", f"/users/{user}/question-workbench", {"enabled": True, "version": 0}),
        ("POST", f"/users/{user}/revoke-sessions", None), ("POST", f"/users/{user}/resend-verification", None),
    ]:
        # Member is an administrator of their own workspace, not of the site.
        assert member.request(method, "/site/admin"+path, json=body).status_code == 403
        assert anonymous.request(method, "/site/admin"+path, json=body).status_code == 401
    for path in ("/workspaces", f"/workspaces/{uuid4()}"):
        assert admin.get("/site/admin" + path).status_code == 404
    db.get(User, UUID(a["user"]["id"])).is_site_admin = False; db.commit()
    assert admin.get("/site/admin/overview").status_code == 403


def test_lists_filters_paging_and_read_only_redaction(accounts):
    admin, member, _, db, a, b = setup(accounts)
    target = db.get(User, UUID(b["user"]["id"]))
    target.email_verified_at = auth.now()
    db.add(ExternalIdentity(provider="senyu:test:wx", subject="private-social-id", user_id=target.id))
    provider = SiteModelProvider(key="test", name="Provider", model="private-model",
        connection_id=connection_id(db, url="https://secret-host", secret="never-return-this"), enabled=True)
    db.add(provider)
    db.flush()
    account = db.get(UserCreditAccount, target.id)
    account.balance = 1000
    account.lifetime_granted = 1000
    account.lifetime_spent = 0
    db.add(UserModelPolicy(user_id=target.id, available_models=[str(provider.id)],
        default_model=str(provider.id), billing_multiplier_percent=125, version=2))
    db.commit()
    users = admin.get("/site/admin/users?q=Member&verified=yes&status=active&page_size=1").json()
    assert users["total"] == 1 and users["items"][0]["id"] == str(target.id)
    assert admin.get("/site/admin/users?q=%25").json()["total"] == 0
    assert admin.get("/site/admin/users?page_size=1&page=2").json()["page"] == 2
    assert admin.get("/site/admin/users?verified=invalid").status_code == 422
    assert admin.get("/site/admin/users?page_size=101").status_code == 422
    detail = admin.get(f"/site/admin/users/{target.id}")
    assert detail.json()["login_methods"]["wechat"] is True
    assert detail.json()["credits"]["balance"] == 1000
    assert detail.json()["model_policy"] == {
        "available_models": [str(provider.id)],
        "default_model": str(provider.id),
        "billing_multiplier_percent": 125,
        "version": 2,
    }
    assert "workspaces" not in detail.json()
    for secret in ("password_hash", "private-social-id", "never-return-this", "secret-host", "csrf_token"):
        assert secret not in detail.text
    workspace_path = f"/site/admin/workspaces/{uuid4()}"
    assert admin.get(workspace_path).status_code == 404
    assert admin.put(workspace_path, json={"balance": 999}).status_code == 404
    assert admin.get("/site/admin/overview").json()["users_total"] == 2


def test_suspend_reactivate_revocation_and_self_protection(accounts):
    admin, member, _, db, a, b = setup(accounts)
    target = b["user"]["id"]
    access = auth.get_access(db.get(LoginSession, auth.digest(member.cookies.get(auth.COOKIE))))
    assert admin.put(f"/site/admin/users/{a['user']['id']}/status", json={"suspended": True}).status_code == 409
    assert admin.put(f"/site/admin/users/{target}/status", json={"suspended": True, "reason_code": "security"}).status_code == 200
    assert member.get("/auth/me").status_code == 401
    with pytest.raises(HTTPException): access.revalidate(db)
    assert member.post("/auth/login", json={"email": b["user"]["email"], "password": PASSWORD}).status_code == 403
    assert admin.put(f"/site/admin/users/{target}/status", json={"suspended": False}).status_code == 200
    assert member.post("/auth/login", json={"email": b["user"]["email"], "password": PASSWORD}).status_code == 200
    assert admin.post(f"/site/admin/users/{target}/revoke-sessions").json()["revoked"] == 1
    assert member.get("/auth/me").status_code == 401
    actions = [row.action for row in db.scalars(select(SiteAuditEvent))]
    assert all(action in actions for action in ("user.suspended", "user.reactivated", "user.sessions_revoked", "auth.login_blocked_suspended"))


def test_admin_resend_confirmation_single_use_and_failure(accounts, monkeypatch):
    admin, member, anonymous, db, a, b = setup(accounts)
    target = b["user"]["id"]
    db.add(SiteAuthSettings(id=1, config={"smtp": {"enabled": True, "host": "smtp.example.com", "from_address": "mail@example.com"}})); db.commit()
    sent = []
    monkeypatch.setattr(site_admin, "email_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr(site_admin, "send_mail", lambda *_args: sent.append(_args[3]))
    assert admin.post(f"/site/admin/users/{target}/resend-verification").status_code == 200
    assert admin.post(f"/site/admin/users/{target}/resend-verification").status_code == 429
    token = sent[0].split("#token=")[1].split()[0]
    assert token not in admin.get("/site/admin/audit").text
    assert anonymous.post("/auth/email/confirm-link", json={"token": token}).status_code == 200
    assert anonymous.post("/auth/email/confirm-link", json={"token": token}).status_code == 400
    user = db.get(User, UUID(target)); assert user.email_verified_at and user.password_hash is None
    assert member.get("/auth/me").status_code == 401
    assert not db.scalars(select(AuthToken).where(AuthToken.purpose == "site_verify")).all()
    actor = db.get(User, UUID(a["user"]["id"]))
    def failed(*_a): raise HTTPException(503, "邮件发送失败")
    monkeypatch.setattr(site_admin, "send_mail", failed)
    assert admin.post(f"/site/admin/users/{actor.id}/resend-verification").status_code == 503
    assert not db.scalars(select(AuthToken).where(AuthToken.user_id == actor.id)).all()
    assert admin.get("/site/admin/audit?action=user.verification_resent&result=failed").json()["total"] >= 1


def test_audit_allowlist_filters_retention_and_no_editing(accounts):
    admin, _, _, db, a, _ = setup(accounts)
    user = db.get(User, UUID(a["user"]["id"]))
    record(db, user, "test.safe", "user", user.id, details={"password": "do-not-store", "code": "123456", "source": "test"})
    old = SiteAuditEvent(action="old", target_type="user", created_at=auth.now()-timedelta(days=366)); db.add(old); db.commit(); old_id = old.id
    result = admin.get(f"/site/admin/audit?action=test.safe&actor_id={user.id}&result=success&page_size=1").json()
    assert result["total"] == 1 and result["items"][0]["details"] == {"source": "test"}
    assert admin.get("/site/admin/audit?action=old").json()["total"] == 0
    assert prune(db) == 1; db.commit(); assert db.get(SiteAuditEvent, old_id) is None
    assert admin.delete("/site/admin/audit").status_code == 405
    assert admin.put("/site/admin/audit", json={}).status_code == 405
