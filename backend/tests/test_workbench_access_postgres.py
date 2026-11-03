from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, site_admin
from app.models import QuestionWorkbenchGrant, User
from test_auth_postgres import auth_postgres


def test_concurrent_entitlement_changes_keep_one_version(auth_postgres):
    with Session(auth_postgres) as db:
        actor, workspace = auth.create_user(db, f"qw-access-{uuid4().hex}@example.com", "Teacher")
        db.commit()
        actor_id = actor.id
        assert actor.question_workbench_enabled is False
        db.add(QuestionWorkbenchGrant(user_id=actor_id, workspace_id=workspace, enabled=True, version=4))
        db.commit()
    barrier = Barrier(2)

    def enable():
        with Session(auth_postgres) as db:
            actor = db.get(User, actor_id)
            barrier.wait(timeout=10)
            try:
                site_admin.set_workbench_access(actor_id, site_admin.WorkbenchAccess(enabled=True, version=0), None, db, actor)
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enable(), range(2)))
    assert sorted(results) == [200, 409]
    with Session(auth_postgres) as db:
        user = db.get(User, actor_id)
        assert user.question_workbench_enabled and user.question_workbench_version == 1
        grant = db.scalar(select(QuestionWorkbenchGrant).where(QuestionWorkbenchGrant.user_id == actor_id))
        assert not grant.enabled and grant.version == 5
