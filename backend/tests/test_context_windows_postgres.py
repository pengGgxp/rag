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
        user, workspace = auth.create_user(db, f"window-{uuid4().hex}@example.test", "Window test")
        raw = uuid4().hex
        db.add(LoginSession(token_hash=auth.digest(raw), user_id=user.id, workspace_id=workspace,
            csrf_token="test-only", expires_at=auth.now() + timedelta(hours=1)))
        shared = SiteModelProvider(key=uuid4().hex, name="Local fake", model="test", connection_id=connection_id(db))
        db.add(shared); db.flush()
        binding = LLMProviderConfig(workspace_id=workspace, site_provider_id=shared.id,
            key="test", name="Local fake", model="test", base_url="", encrypted_api_key="")
        conversation = Conversation(workspace_id=workspace, owner_id=user.id, active_request_id=uuid4())
        db.add_all([binding, conversation]); db.flush()
        access = Access(user.id, workspace, "admin", auth.digest(raw))
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
