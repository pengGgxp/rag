from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import model_pricing as pricing
from app.auth import create_user
from app.models import ModelBillingSettings, UsagePolicy, User, Workspace
from test_auth_postgres import auth_postgres


def test_template_concurrent_save_and_transactional_copy(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        row = pricing.settings_locked(db)
        version = row.version
        actor = User(email=f"template-{uuid4().hex}@example.com", name="Template", is_site_admin=True)
        db.add(actor); db.commit(); actor_id = actor.id
    barrier = Barrier(3)
    def update(rate):
        with Session(engine) as db:
            payload = pricing.TemplateUpdate(version=version, policies=[pricing.TemplatePrice(
                capability=key, unit="pages" if key == "ocr_pages" else "tokens", unit_size=1000, credits_per_unit=rate) for key in pricing.TEMPLATE_CAPABILITIES])
            barrier.wait(timeout=10)
            try:
                pricing.update_template(payload, db, db.get(User, actor_id))
                return 200
            except HTTPException as exc:
                db.rollback(); return exc.status_code
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(update, [2,3,4]))
    assert results.count(200) == 1 and results.count(409) == 2
    with Session(engine) as db:
        _, workspace = create_user(db, f"copy-{uuid4().hex}@example.com", "Copy")
        copied = db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id==workspace)).all()
        assert len(copied) == 2
        assert {row.template_version for row in copied} == {version+1}
        assert len({row.credits_per_unit for row in copied}) == 1
        db.rollback()
    with Session(engine) as db:
        assert db.get(Workspace, workspace) is None
        assert not db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id==workspace)).all()
        # Do not leak this test's template into subsequent migrated-database tests.
        settings = db.get(ModelBillingSettings, 1)
        settings.template = {}; settings.version += 1; db.commit()
