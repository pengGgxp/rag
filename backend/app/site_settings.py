"""Site operations settings. Public reads never expose announcement drafts."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import current_session
from .db import get_db
from .models import SiteSettings
from .site_auth import site_admin
from .site_audit import record

router = APIRouter(tags=["site-settings"])
DEFAULT_SIGNUP_CREDITS = 1000


def signup_grant(db):
    row = db.get(SiteSettings, 1, populate_existing=True)
    return (row.signup_credits, row.version) if row else (DEFAULT_SIGNUP_CREDITS, 0)


def read_settings(row):
    return {
        "version": row.version if row else 0,
        "signup_credits": row.signup_credits if row else DEFAULT_SIGNUP_CREDITS,
        "announcement_title": row.announcement_title if row else "",
        "announcement_body": row.announcement_body if row else "",
        "administrator_contact": row.administrator_contact if row else "",
        "announcement_published": row.announcement_published if row else False,
        "announcement_revision": row.announcement_revision if row else 0,
        "announcement_updated_at": row.announcement_updated_at if row else None,
    }


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    version: int = Field(ge=0)
    signup_credits: int = Field(ge=0, le=1_000_000)
    announcement_title: str = Field(max_length=120)
    announcement_body: str = Field(max_length=10_000)
    announcement_published: bool
    administrator_contact: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def complete_published_announcement(self):
        if self.announcement_published and (not self.announcement_title or not self.announcement_body):
            raise ValueError("发布公告需要填写标题和正文")
        return self


@router.get("/site/admin/settings")
def get_settings(db: Session = Depends(get_db), actor=Depends(site_admin)):
    return read_settings(db.get(SiteSettings, 1, populate_existing=True))


@router.put("/site/admin/settings")
def save_settings(payload: SettingsInput, request: Request, db: Session = Depends(get_db), actor=Depends(site_admin)):
    # Serialize both first-time configuration and version checks.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020048)"))
    row = db.get(SiteSettings, 1, with_for_update=True, populate_existing=True)
    before = read_settings(row)
    if before["version"] != payload.version:
        raise HTTPException(409, "站点设置已更新，请重新加载后再保存")
    changes = [key for key, value in payload.model_dump(exclude={"version"}).items() if before[key] != value]
    if not changes:
        return before
    if row is None:
        row = SiteSettings(id=1, version=0, announcement_revision=0)
        db.add(row)
    for key in changes:
        setattr(row, key, getattr(payload, key))
    if any(key.startswith("announcement_") for key in changes):
        row.announcement_revision = before["announcement_revision"] + 1
        row.announcement_updated_at = datetime.now(timezone.utc)
    row.version = before["version"] + 1
    record(db, actor, "site.settings_updated", "site_settings", 1, request=request,
        details={"fields": changes, "version": row.version,
            **({"previous_signup_credits": before["signup_credits"], "signup_credits": row.signup_credits}
               if "signup_credits" in changes else {})})
    db.commit()
    return read_settings(row)


@router.get("/site/announcement")
def published_announcement(db: Session = Depends(get_db), session=Depends(current_session)):
    row = db.get(SiteSettings, 1, populate_existing=True)
    if row is None or not row.announcement_published:
        return {"announcement": None}
    return {"announcement": {
        "revision": row.announcement_revision, "title": row.announcement_title,
        "body": row.announcement_body, "updated_at": row.announcement_updated_at,
    }}


@router.get("/site/contact")
def administrator_contact(db: Session = Depends(get_db), session=Depends(current_session)):
    row = db.get(SiteSettings, 1, populate_existing=True)
    return {"administrator_contact": row.administrator_contact if row else ""}
