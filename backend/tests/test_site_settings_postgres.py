"""Run only against the disposable migrated AUTH_TEST_DATABASE_URL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import auth, main
from app.models import UserCreditAccount, SiteSettings
from test_auth_postgres import auth_postgres


@pytest.fixture
def site_settings_postgres(auth_postgres):
    fields = ("version", "signup_credits", "announcement_title", "announcement_body",
        "announcement_published", "announcement_revision", "announcement_updated_at")
    with Session(auth_postgres) as db:
        row = db.get(SiteSettings, 1)
        original = {field: getattr(row, field) for field in fields}
    try:
        yield auth_postgres
    finally:
        with Session(auth_postgres) as db:
            row = db.get(SiteSettings, 1)
            for field, value in original.items():
                setattr(row, field, value)
            db.commit()


def test_migration_defaults_and_existing_balances(site_settings_postgres):
    with Session(site_settings_postgres) as db:
        settings = db.get(SiteSettings, 1)
        assert settings and settings.signup_credits == 1000 and settings.version == 0
        assert not settings.announcement_published and settings.announcement_body == ""


def test_concurrent_settings_updates_reject_stale_version(site_settings_postgres):
    with Session(site_settings_postgres) as db:
        user = auth.create_user(db, f"admin-{uuid4().hex}@example.com", "Admin")
        account_id = user.id
        user.is_site_admin = True
        user.email_verified_at = auth.now()
        response = auth.login_response(db, user)
        import json
        session = json.loads(response.body)
        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        config = db.get(SiteSettings, 1)
        version = config.version
        balance = db.get(UserCreditAccount, account_id).balance
    barrier = Barrier(2)
    def save(value):
        with TestClient(main.app, headers={"X-RAG-Request": "1", "X-CSRF-Token": session["csrf_token"]}) as client:
            client.cookies.set(auth.COOKIE, cookie)
            barrier.wait(timeout=10)
            return client.put("/site/admin/settings", json={
                "version": version, "signup_credits": value,
                "announcement_title": "Concurrent", "announcement_body": "Body", "announcement_published": True,
            }).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, (22, 33))) == [200, 409]
    with Session(site_settings_postgres) as db:
        assert db.get(UserCreditAccount, account_id).balance == balance
        config = db.get(SiteSettings, 1)
        assert config.signup_credits in (22, 33) and config.version == version + 1
        assert config.announcement_revision == 1
        config.signup_credits = 0
        db.commit()


def test_concurrent_new_users_respect_zero_grant(site_settings_postgres):
    with Session(site_settings_postgres) as db:
        config = db.get(SiteSettings, 1)
        config.signup_credits = 0
        db.commit()
    barrier = Barrier(3)
    def create(index):
        with Session(site_settings_postgres) as db:
            barrier.wait(timeout=10)
            account_id = auth.create_user(db, f"zero-{index}-{uuid4().hex}@example.com", "Zero").id
            db.commit()
            return db.get(UserCreditAccount, account_id).balance
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(create, range(3))) == [0, 0, 0]
