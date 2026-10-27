from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import workspace_billing as pricing
from app.models import BillingSnapshot, ModelBillingSettings, Workspace
from test_auth_postgres import auth_postgres


def test_concurrent_snapshots_are_immutable_and_unique(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        settings = db.get(ModelBillingSettings, 1)
        if settings:
            settings.pricing_paused = False
            settings.shared_pricing_enabled = False
        workspace = Workspace(name="Snapshot concurrency")
        db.add(workspace); db.commit()
        identifier, request = workspace.id, uuid4()
    barrier = Barrier(4)
    def capture(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            row = pricing.operation_snapshot(db, identifier, request, "model")
            db.commit()
            return row.id, row.context
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(capture, range(4)))
    assert len({result[0] for result in results}) == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BillingSnapshot).where(
            BillingSnapshot.workspace_id == identifier)) == 1
        workspace = db.get(Workspace, identifier)
        workspace.billing_multiplier_percent = 125
        db.commit()
        row = pricing.operation_snapshot(db, identifier, request, "model")
        assert row.context == results[0][1] and row.context["multiplier_percent"] == 100
        row.status = "complete"
        db.commit()
