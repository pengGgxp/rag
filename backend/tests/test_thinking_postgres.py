"""Run only against the disposable migrated authentication test database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ChatMessage, Conversation, SiteModelProvider, Workspace
from app.shared_providers import lock_provider
from app.thinking import select_thinking
from test_auth_postgres import auth_postgres
from test_thinking import effort


def test_migration_defaults_and_message_snapshot_roundtrip(auth_postgres):
    with Session(auth_postgres) as db:
        row = SiteModelProvider(key=f"thinking-{uuid4()}", name="Test", model="model",
            base_url="https://example.test/v1", encrypted_api_key="encrypted")
        workspace = Workspace(name="Thinking migration")
        db.add_all([row, workspace])
        db.flush()
        conversation = Conversation(title="Test", workspace_id=workspace.id)
        # Owner is nullable in the legacy conversation schema.
        db.add(conversation)
        db.flush()
        message = ChatMessage(conversation_id=conversation.id, sequence=0, role="assistant", content="legacy")
        db.add(message)
        db.commit()
        assert row.thinking_config == {} and row.thinking_version == 0
        assert message.thinking_snapshot is None and message.encrypted_continuation is None
        row.thinking_config, row.thinking_version = effort(), 1
        message.thinking_snapshot = select_thinking(effort(), 1).snapshot()
        message.encrypted_continuation = "opaque-ciphertext"
        db.commit()
        db.expire_all()
        assert db.get(ChatMessage, message.id).thinking_snapshot["config_version"] == 1
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0028_message_thinking"


def test_concurrent_admin_edits_reject_stale_version(auth_postgres):
    with Session(auth_postgres) as db:
        row = SiteModelProvider(key=f"concurrent-{uuid4()}", name="Test", model="model",
            base_url="https://example.test/v1", encrypted_api_key="encrypted")
        db.add(row)
        db.commit()
        identifier = row.id
    barrier = Barrier(2)
    def edit(value):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                row = lock_provider(db, identifier, 1)
                row.thinking_config, row.thinking_version = effort(value=value), 1
                row.version += 1
                db.commit()
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["low", "high"])) == [200, 409]
