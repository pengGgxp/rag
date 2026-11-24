"""Admission and settlement races against a disposable migrated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Lock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.model_catalog import providers as llm_providers
from app.billing import workspace as workspace_billing
from app.files import workspace as workspace_files
from app.images import adapters as image_adapters, tasks as image_tasks
from app.identity.auth import Access, digest, now
from app.billing.credits import ensure_account
from app.images.schemas import ImageInput
from app.model_catalog.capabilities import ImageConfig
from app.models import AgentToolExecution, Conversation, UserCreditAccount, ImageTask, LoginSession, ModelConnection, SiteModelProvider, UsageEvent, User, WorkspaceFile
from test_auth_postgres import auth_postgres
from test_image_tasks import png


@pytest.mark.parametrize("inline", [False, True])
def test_concurrent_image_admission_and_worker_claim_settle_once(auth_postgres, monkeypatch, inline):
    engine = auth_postgres
    execution_id = uuid4() if inline else None
    with Session(engine) as db:
        user = User(email=f"image-{uuid4().hex}@example.com", name="Image test", email_verified_at=now())
        db.add(user); db.flush()
        token = digest(uuid4().hex)
        db.add(LoginSession(token_hash=token, user_id=user.id,
            csrf_token="test-only", expires_at=now()+timedelta(hours=1)))
        connection = ModelConnection(name="Synthetic image", base_url="https://example.invalid/v1", encrypted_api_key="enc:key")
        db.add(connection); db.flush()
        config = ImageConfig.model_validate({"generate": True, "specs": [{"operation": "generate", "size": "1024x1024", "quality": "standard", "credits_per_image": "3.25"}]})
        model = SiteModelProvider(key=uuid4().hex, name="Synthetic image", model="synthetic", connection_id=connection.id,
            supports_chat=False, enabled=True, image_config=config.model_dump(mode="json"))
        db.add(model); db.flush()
        if inline:
            request_id = uuid4()
            conversation = Conversation(account_id=user.id, owner_id=user.id, active_request_id=request_id)
            db.add(conversation); db.flush()
            db.add(AgentToolExecution(id=execution_id, conversation_id=conversation.id,
                request_id=request_id, model_item_id=uuid4(), call_id="once", name="generate_image"))
        ensure_account(db, user.id, initial=100)
        db.commit()
        uid, wid, mid, cid = user.id, user.id, model.id, connection.id
    def dispatch(_):
        assert not inline, "Agent 任务不得二次派发"
    monkeypatch.setattr(image_tasks, "dispatch", dispatch)
    monkeypatch.setattr(image_tasks, "selected_model", lambda db, _: (db.get(SiteModelProvider, mid), db.get(ModelConnection, cid), config))
    monkeypatch.setattr(workspace_billing, "context_for_workspace", lambda db, identifier: {"multiplier_percent": 125, "workspace_id": str(identifier)})
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: "enc:"+value)
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("enc:"))
    access = Access(uid, token)
    barrier = Barrier(4)
    key = uuid4().hex
    def submit(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return image_tasks.create(db, access, ImageInput(prompt="synthetic"), key,
                agent_tool_execution_id=execution_id)["task_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(submit, range(4)))
    assert len(set(identifiers)) == 1
    calls, uploaded, lock = [], {}, Lock()
    def generate(*_):
        with lock:
            calls.append(1)
        return [(png(), "image/png")], "synthetic-request", {}
    monkeypatch.setattr(image_adapters, "generate", generate)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, mime: uploaded.update({key: stream.read()}))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: image_tasks.run(identifiers[0], factory,
            agent_tool_execution_id=execution_id), range(4)))
    assert len(calls) == 1
    with Session(engine) as db:
        task = db.get(ImageTask, UUID(identifiers[0]))
        assert task.status == "completed", task.error
        assert task.charged_credits == Decimal("4.0625")
        assert db.get(UserCreditAccount, wid).balance == Decimal("95.9375")
        assert len(db.scalars(select(UsageEvent).where(UsageEvent.request_id == task.id)).all()) == 1
        assert len(db.scalars(select(WorkspaceFile).where(WorkspaceFile.account_id == wid)).all()) == 1
        assert len(uploaded) == 1
