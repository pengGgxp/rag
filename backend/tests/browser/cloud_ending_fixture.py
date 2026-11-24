"""Loopback-only acceptance controls; never imported by the application entrypoint."""
from contextlib import asynccontextmanager
from decimal import Decimal

import cloud_experience_fixture as fixture
from app import cloud_computer as cloud
from app.identity.auth import Access, digest
from app.models import CloudPreview, UserCreditAccount

app = fixture.app


@asynccontextmanager
async def isolated_lifespan(_app):
    # Cases invoke maintenance explicitly; background timers would obscure confirmation.
    yield


app.router.lifespan_context = isolated_lifespan
cloud.dispatch = lambda *_: None


@app.post("/__test__/scenario/{case}")
def scenario(case: str):
    if case not in ("ready", "delayed", "insufficient", "legacy"):
        raise ValueError("Unknown isolated test case")
    fixture.fake.kill_uncertain = False
    cloud.maintenance()
    with fixture.SessionLocal() as db:
        old = db.scalar(cloud.select(cloud.Computer).where(
            cloud.Computer.conversation_id == fixture.conversation_id,
            cloud.Computer.ended_at.is_(None)))
        if old:
            old.status, old.billing_stopped_at = "stopping", cloud.now()
            db.commit()
    cloud.maintenance()
    with fixture.SessionLocal() as db:
        access = Access(fixture.UUID(fixture.identity["user"]["id"]), digest(fixture.client.cookies.get(fixture.auth.COOKIE)))
        db.get(UserCreditAccount, access.user_id).balance = Decimal("100000")
        settings = db.get(fixture.CloudComputerSettings, 1)
        settings.config = {**settings.config, "credits_per_minute": "2"}
        db.commit()
        command = cloud.submit(db, access, fixture.conversation_id, fixture.uuid4(), {"command": "echo synthetic"})
        command_id = command.id
    cloud.run(command_id)
    with fixture.SessionLocal() as db:
        command = db.get(fixture.CloudComputerCommand, command_id)
        row = db.get(cloud.Computer, command.session_id)
        row.started_at = cloud.now() - fixture.timedelta(seconds=601 if case == "delayed" else 61)
        row.last_activity_at = cloud.now()
        if case == "insufficient":
            db.get(UserCreditAccount, row.account_id).balance = 0
        preview = db.get(CloudPreview, fixture.UUID(fixture.receipt["preview_id"]))
        preview.session_id, preview.command_id = row.id, command.id
        preview.status, preview.ended_at, preview.error = "ready", None, None
        if case == "legacy":
            row.status, row.ended_at = "ended", cloud.now()
            row.error = "积分不足，云电脑正在结束"
            cloud.reconcile(db, row, cloud.now(), final=True)
            row.stop_reason = None
            preview.status, preview.ended_at = "ended", cloud.now()
        db.commit()
    fixture.fake.kill_uncertain = case in ("delayed", "insufficient")
    if case in ("delayed", "insufficient"):
        cloud.maintenance()
    return {"conversation_id": str(fixture.conversation_id), "preview_id": fixture.receipt["preview_id"]}


@app.post("/__test__/confirm-end")
def confirm_end():
    fixture.fake.kill_uncertain = False
    cloud.maintenance()
    return {"confirmed": True}
