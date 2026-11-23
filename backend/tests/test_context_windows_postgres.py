"""Real PostgreSQL row locks and CAS; never uses a deployment database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy.orm import Session

from app import auth
from app.auth import Access
from app.context_windows import ContextWindowError, WindowStore, make_item
from app.llm import ChatProviderConfig
from app.models import Conversation, LoginSession, LLMProviderConfig, SiteModelProvider
from auth_helpers import connection_id
from test_auth_postgres import auth_postgres


def test_postgres_install_serializes_competing_replacements(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"window-{uuid4().hex}@example.test", "Window test")
        workspace = user.id
        raw = uuid4().hex
        db.add(LoginSession(token_hash=auth.digest(raw), user_id=user.id,
            csrf_token="test-only", expires_at=auth.now() + timedelta(hours=1)))
        shared = SiteModelProvider(key=uuid4().hex, name="Local fake", model="test", connection_id=connection_id(db))
        db.add(shared); db.flush()
        binding = LLMProviderConfig(account_id=workspace, site_provider_id=shared.id,
            key="test", name="Local fake", model="test", base_url="", encrypted_api_key="")
        conversation = Conversation(account_id=workspace, owner_id=user.id, active_request_id=uuid4())
        db.add_all([binding, conversation]); db.flush()
        access = Access(user.id, auth.digest(raw))
        config = ChatProviderConfig("", "", "test", provider_id=str(binding.id))
        store = WindowStore(access, conversation.id, config)
        state, _ = store.load(db)
        revision, window, request = state.revision, state.window_id, conversation.active_request_id
        db.commit()
    barrier = Barrier(2)
    def candidate(text):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                state, _ = store.install(db, [make_item(uuid4(), {"role": "user", "content": text}, "handoff")],
                    request_id=request, revision=revision, window_id=window, install_id=uuid4(),
                    summary=text, before=100, after=10, budget={"hard_input": 1000})
                db.commit()
                return "installed"
            except ContextWindowError as exc:
                db.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(candidate, ["one", "two"]))
    assert outcomes == ["context_revision_conflict", "installed"]
    with Session(engine) as db:
        state, items = store.load(db)
        assert state.revision == revision + 1
        assert len(items) == 1


def test_postgres_tool_admission_is_once_and_late_success_survives_stop(auth_postgres):
    from sqlalchemy import select, func
    from sqlalchemy.orm import sessionmaker
    from app.agent.loop import AgentCancelled
    from app.agent.effective_context import ContextManager
    from app.llm import ToolCall
    from app.models import AgentToolExecution
    import pytest
    engine = auth_postgres
    request = uuid4()
    with Session(engine) as db:
        user = auth.create_user(db, f"tool-{uuid4().hex}@example.test", "Tool test")
        workspace = user.id
        raw = uuid4().hex
        db.add(LoginSession(token_hash=auth.digest(raw), user_id=user.id,
            csrf_token="test-only", expires_at=auth.now() + timedelta(hours=1)))
        conversation = Conversation(account_id=workspace, owner_id=user.id, active_request_id=request)
        db.add(conversation); db.flush()
        access = Access(user.id, auth.digest(raw))
        manager = ContextManager(sessionmaker(bind=engine), access, conversation.id,
            ChatProviderConfig("", "", "test"), None, None, request)
        manager.history(db, conversation); db.commit()
    manager.start({"role": "system", "content": "system"})
    call = ToolCall("once", "write_file", {})
    manager.record([{"role": "assistant", "content": None, "tool_calls": [
        {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": "{}"}}]}], "model", "model:1")
    barrier = Barrier(2)
    def admit(_):
        barrier.wait(timeout=10)
        try:
            return manager.begin_tool(call)
        except ContextWindowError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))
    assert results.count("tool_execution_already_started") == 1
    identifier = next(value for value in results if value != "tool_execution_already_started")
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AgentToolExecution).where(
            AgentToolExecution.conversation_id == manager.conversation_id)) == 1
        db.get(Conversation, manager.conversation_id).active_request_id = None
        db.commit()
    with pytest.raises(AgentCancelled):
        manager.finish_tool(identifier, {"role": "tool", "tool_call_id": call.id,
            "name": call.name, "content": '{"saved": true}'}, 1000)
    with Session(engine) as db:
        assert db.get(AgentToolExecution, identifier).status == "completed"


def test_postgres_steering_admission_and_stop_are_atomic(auth_postgres):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from app import main
    from app.conversations import jobs as conversation_jobs
    from app.agent.effective_context import ContextManager
    from app.models import ChatMessage

    engine = auth_postgres
    request_id, supplement = uuid4(), str(uuid4())
    with Session(engine) as db:
        user = auth.create_user(db, f"steering-{uuid4().hex}@example.test", "Steering test")
        token = uuid4().hex
        db.add(LoginSession(token_hash=auth.digest(token), user_id=user.id,
            csrf_token="test-only", expires_at=auth.now() + timedelta(hours=1)))
        conv = Conversation(account_id=user.id, owner_id=user.id, active_request_id=request_id, next_sequence=2)
        db.add(conv)
        db.flush()
        conv_id = conv.id
        original = ChatMessage(conversation_id=conv_id, role="user", sequence=0, content="Original question")
        answer = ChatMessage(conversation_id=conv_id, role="assistant", sequence=1, request_id=request_id, status="streaming")
        db.add_all([original, answer])
        db.flush()
        access = Access(user.id, auth.digest(token))
        config = ChatProviderConfig("", "", "test")
        manager = ContextManager(sessionmaker(bind=engine), access, conv_id, config, None, None, request_id)
        conversation_jobs.enqueue(db, conv_id, answer.id, request_id, access, 0, config)
        manager.history(db, conv)
        db.commit()
        conversation_jobs.update_steering(db, conv, request_id, supplement, {"text": "More details"})
    manager.start({"role": "system", "content": "System"})
    barrier = Barrier(2)

    def admit():
        barrier.wait(timeout=10)
        return manager.steer(supplement)

    def stop():
        with TestClient(main.app, headers={"X-RAG-Request": "1", "X-CSRF-Token": "test-only"}) as client:
            client.cookies.set(auth.COOKIE, token)
            barrier.wait(timeout=10)
            return client.post(f"/conversations/{conv_id}/stop", json={"request_id": str(request_id)})

    with ThreadPoolExecutor(max_workers=2) as pool:
        admission, stopping = pool.submit(admit), pool.submit(stop)
        admitted = admission.result(timeout=10) is not None
        stopped = stopping.result(timeout=10)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"
    assert any(block["id"] == supplement for block in stopped.json()["display_blocks"]) == admitted
    with Session(engine) as db:
        _, items = manager.store.load(db)
        assert sum(item["message_id"] == supplement for item in items) == int(admitted)
        assert len(conversation_jobs.accepted_steering(db, request_id)) == int(admitted)
    assert manager.steer(supplement) is None
