from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app import auth, email_auth
from app.models import (
    AuthenticationFlow, CreditAccount, CreditLedgerEntry, SiteAuditEvent, SiteSettings, User,
)
from test_auth import PASSWORD, register, share, switch

ROOT = "/site/admin/settings"


def admin(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    return owner, db, session


def save(client, **changes):
    payload = client.get(ROOT).json()
    for field in ("announcement_revision", "announcement_updated_at"):
        payload.pop(field, None)
    return client.put(ROOT, json={**payload, **changes})


def test_admin_permissions_csrf_and_live_revocation(accounts):
    owner, db, session = admin(accounts)
    (_, member, anonymous), _ = accounts
    register(member, "member@example.com")
    for client, code in ((member, 403), (anonymous, 401)):
        assert client.get(ROOT).status_code == code
        assert client.put(ROOT, json={"version": 0}).status_code == code
    assert owner.put(ROOT, json={}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    db.get(User, UUID(session["user"]["id"])).is_site_admin = False
    db.commit()
    assert owner.get(ROOT).status_code == 403
    assert anonymous.get("/site/announcement").status_code == 401


def test_default_and_configured_registration_only_affects_new_accounts(accounts):
    owner, db, first = admin(accounts)
    assert owner.get(ROOT).json()["signup_credits"] == 1000
    assert save(owner, signup_credits=432).status_code == 200
    (_, second, _), _ = accounts
    result = register(second, "second@example.com")
    assert second.get("/usage").json()["balance"] == 432
    assert owner.get("/usage").json()["balance"] == 1000
    ledger = db.scalars(select(CreditLedgerEntry).where(
        CreditLedgerEntry.workspace_id == UUID(result["workspace_id"]))).all()
    assert len(ledger) == 1 and ledger[0].amount == 432
    assert ledger[0].metadata_json["site_settings_version"] == 1
    assert second.post("/auth/register", json={
        "email": "second@example.com", "name": "Again", "password": PASSWORD,
    }).status_code == 409
    login = second.post("/auth/login", json={"email": "second@example.com", "password": PASSWORD})
    assert login.status_code == 200
    second.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert second.get("/usage").json()["balance"] == 432
    share(owner, "second@example.com")
    switch(second, first["workspace_id"])
    assert second.get("/usage").json()["balance"] == 1000
    switch(second, result["workspace_id"])
    assert second.get("/usage").json()["balance"] == 432


def test_zero_grant_remains_zero_after_usage_and_login(accounts):
    owner, db, _ = admin(accounts)
    assert save(owner, signup_credits=0).status_code == 200
    (_, second, _), _ = accounts
    result = register(second, "zero@example.com")
    workspace_id = UUID(result["workspace_id"])
    assert second.get("/usage").json()["balance"] == 0
    assert second.get("/usage").json()["balance"] == 0
    assert db.get(CreditAccount, workspace_id).lifetime_granted == 0
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
        CreditLedgerEntry.workspace_id == workspace_id)) == 0


@pytest.mark.parametrize("amount", [-1, 1000001, 1.5, True, "1000", None])
def test_invalid_grant_rejected_without_changes(accounts, amount):
    owner, _, _ = admin(accounts)
    assert save(owner, signup_credits=amount).status_code == 422
    assert owner.get(ROOT).json()["signup_credits"] == 1000
    assert owner.get(ROOT).json()["version"] == 0


def test_upper_limit_and_version_conflict(accounts):
    owner, _, _ = admin(accounts)
    assert save(owner, signup_credits=1000000).status_code == 200
    result = save(owner, version=0, signup_credits=5)
    assert result.status_code == 409
    assert owner.get(ROOT).json()["signup_credits"] == 1000000
    # An identical save is a no-op, not a new publication.
    assert save(owner).json()["version"] == 1


def test_announcement_drafts_publication_revision_and_audit(accounts):
    owner, db, _ = admin(accounts)
    (_, member, _), _ = accounts
    register(member, "reader@example.com")
    assert member.get("/site/announcement").json() == {"announcement": None}
    draft = save(owner, announcement_title="标题", announcement_body="<script>alert(1)</script>\n第二行")
    assert draft.status_code == 200
    assert member.get("/site/announcement").json() == {"announcement": None}
    assert save(owner, announcement_published=True).status_code == 200
    published = member.get("/site/announcement").json()["announcement"]
    assert published["title"] == "标题" and published["revision"] == 2
    assert published["body"] == "<script>alert(1)</script>\n第二行"
    assert set(published) == {"title", "body", "revision", "updated_at"}
    assert save(owner, signup_credits=500).status_code == 200
    assert member.get("/site/announcement").json()["announcement"]["revision"] == 2
    assert save(owner, announcement_body="新公告").status_code == 200
    assert member.get("/site/announcement").json()["announcement"]["revision"] == 3
    assert save(owner, announcement_published=False).status_code == 200
    assert member.get("/site/announcement").json() == {"announcement": None}
    events = db.scalars(select(SiteAuditEvent).where(SiteAuditEvent.action == "site.settings_updated")).all()
    assert len(events) == 5
    assert all("script" not in str(row.details) and "新公告" not in str(row.details) for row in events)
    gift_event = next(row for row in events if "signup_credits" in row.details["fields"])
    assert gift_event.details["previous_signup_credits"] == 1000 and gift_event.details["signup_credits"] == 500


@pytest.mark.parametrize("changes", [
    {"announcement_published": True},
    {"announcement_title": "  ", "announcement_body": "body", "announcement_published": True},
    {"announcement_title": "a" * 121},
    {"announcement_body": "a" * 10001},
    {"announcement_published": "true"},
    {"unknown": "extra"},
])
def test_invalid_announcement_rejected(accounts, changes):
    owner, _, _ = admin(accounts)
    assert save(owner, **changes).status_code == 422


def test_verified_and_wechat_registration_use_same_grant(accounts):
    owner, db, _ = admin(accounts)
    assert save(owner, signup_credits=17).status_code == 200
    (_, second, third), _ = accounts
    for client, purpose in ((second, "register"), (third, "wechat")):
        raw = f"site-settings-{purpose}"
        db.add(AuthenticationFlow(token_hash=auth.digest(raw), purpose=purpose, email=f"{purpose}@example.com",
            data={"stage": "profile", **({"identity": {"provider": "test", "subject": "one"}} if purpose == "wechat" else {})},
            expires_at=auth.now() + timedelta(minutes=10)))
        db.commit()
        client.cookies.set(email_auth.FLOW_COOKIE, raw)
        result = client.post("/auth/complete-registration", json={"name": purpose, "password": PASSWORD})
        assert result.status_code == 201, result.text
        account = db.get(CreditAccount, UUID(result.json()["workspace_id"]))
        assert account.balance == account.lifetime_granted == 17
        assert client.post("/auth/complete-registration", json={"name": purpose, "password": PASSWORD}).status_code in (400, 409)


def test_account_creation_rollback_does_not_leave_grant(accounts):
    owner, db, _ = admin(accounts)
    assert save(owner, signup_credits=29).status_code == 200
    user, workspace_id = auth.create_user(db, "rollback@example.com", "Rollback")
    db.flush()
    assert db.get(CreditAccount, workspace_id).balance == 29
    db.rollback()
    assert db.get(CreditAccount, workspace_id) is None
    assert db.scalar(select(User).where(User.email == "rollback@example.com")) is None
