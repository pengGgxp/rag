"""Loopback-only multi-instance UI fixture using synthetic provider calls."""
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import cloud_experience_fixture as fixture
from app import cloud_computer as cloud
from app.auth import Access, digest
from app.models import UserCreditAccount

app = fixture.app
cloud.dispatch = lambda *_: None


@asynccontextmanager
async def isolated_lifespan(_app):
    yield


app.router.lifespan_context = isolated_lifespan


@app.post("/__test__/scenario/multiple")
def multiple():
    names = [f"独立任务甲-{uuid4().hex[:6]}", f"独立任务乙-{uuid4().hex[:6]}"]
    access = Access(fixture.UUID(fixture.identity["user"]["id"]),
        digest(fixture.client.cookies.get(fixture.auth.COOKIE)))
    identifiers = []
    with fixture.SessionLocal() as db:
        account = db.get(UserCreditAccount, access.user_id)
        account.balance = account.lifetime_granted = Decimal("1000000000")
        settings = db.get(fixture.CloudComputerSettings, 1)
        settings.config = {**settings.config, "credits_per_minute": "0"}
        db.commit()
        for name in names:
            created = cloud.create_computer(db, access, fixture.conversation_id, uuid4(), {"name": name})
            identifiers.append(created["computer_id"])
        command = cloud.submit(db, access, fixture.conversation_id, uuid4(),
            {"computer_id": identifiers[0], "command": "echo synthetic pending"}, dispatch_job=False)
        return {"conversation_id": str(fixture.conversation_id), "computer_ids": identifiers, "names": names,
            "command_id": str(command.id), "preview_id": fixture.receipt["preview_id"]}
