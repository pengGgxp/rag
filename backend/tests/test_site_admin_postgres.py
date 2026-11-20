from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import auth
from app.models import LoginSession, User
from test_auth_postgres import auth_postgres


def test_stale_user_cannot_issue_session_after_suspension(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"suspend-{uuid4().hex}@example.com", "Suspend")
        db.commit(); user_id = user.id
    loaded, suspended = Event(), Event()
    def issue():
        with Session(engine) as db:
            user = db.get(User, user_id)
            loaded.set(); assert suspended.wait(10)
            try:
                auth.login_response(db, user)
            except HTTPException as exc:
                return exc.status_code
            return 200
    def suspend():
        assert loaded.wait(10)
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            user.is_suspended = True
            db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            db.commit()
        suspended.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(issue); pool.submit(suspend).result(timeout=15)
        assert result.result(timeout=15) == 403
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == user_id)) == 0


def test_suspension_waits_for_issuance_then_revokes_session(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"serialize-{uuid4().hex}@example.com", "Serialize")
        db.commit(); user_id = user.id
    locked, attempted = Event(), Event()
    def issue():
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            locked.set(); assert attempted.wait(10)
            return auth.login_response(db, user).status_code
    def suspend():
        assert locked.wait(10)
        with Session(engine) as db:
            attempted.set()
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            user.is_suspended = True
            db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(issue); pool.submit(suspend).result(timeout=15)
        assert result.result(timeout=15) == 200
    with Session(engine) as db:
        assert db.get(User, user_id).is_suspended
        assert db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.user_id == user_id)) == 0
