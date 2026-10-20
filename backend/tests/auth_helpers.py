import secrets
from datetime import timedelta

from app.auth import COOKIE, digest, now
from app.models import LoginSession, Membership, User, Workspace, LEGACY_WORKSPACE_ID


def authenticate(http, db):
    """Seed a real server-side session; do not bypass authorization dependencies."""
    db.add(Workspace(id=LEGACY_WORKSPACE_ID, name="test workspace"))
    user = User(email="owner@example.com", name="Owner")
    db.add(user)
    db.flush()
    db.add(Membership(workspace_id=LEGACY_WORKSPACE_ID, user_id=user.id, role="admin"))
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=digest(raw), user_id=user.id, workspace_id=LEGACY_WORKSPACE_ID,
                        csrf_token=csrf, expires_at=now()+timedelta(hours=1)))
    db.commit()
    http.cookies.set(COOKIE, raw)
    http.headers.update({"X-RAG-Request": "1", "X-CSRF-Token": csrf})
    return user
