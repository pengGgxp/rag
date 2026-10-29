"""Run only against the disposable migrated authentication test database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ChatMessage, Conversation, SiteModelProvider, Workspace, LLMProviderConfig, ContextCheckpoint
from app.shared_providers import lock_provider
from app.thinking import select_thinking
from test_auth_postgres import auth_postgres
from test_thinking import effort
from auth_helpers import connection_id


def test_migration_defaults_and_message_snapshot_roundtrip(auth_postgres):
    with Session(auth_postgres) as db:
        row = SiteModelProvider(key=f"thinking-{uuid4()}", name="Test", model="model",
            connection_id=connection_id(db))
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
        message.encrypted_display_thinking = "separate-display-ciphertext"
        message.thinking_diagnostics = [{"round": 1, "received_thinking": True, "reasoning_tokens": 12}]
        db.commit()
        db.expire_all()
        assert db.get(ChatMessage, message.id).thinking_snapshot["config_version"] == 1
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0033_model_connections"
        assert message.encrypted_display_thinking == "separate-display-ciphertext"
        assert message.thinking_diagnostics[0]["reasoning_tokens"] == 12
        assert message.display_blocks == []
        assert row.context_capacity is None and row.max_output_tokens is None


def test_concurrent_admin_edits_reject_stale_version(auth_postgres):
    with Session(auth_postgres) as db:
        row = SiteModelProvider(key=f"concurrent-{uuid4()}", name="Test", model="model",
            connection_id=connection_id(db))
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


def test_ordered_blocks_and_multiple_checkpoints_keep_request_provenance(auth_postgres):
    request_id = uuid4()
    with Session(auth_postgres) as db:
        workspace = Workspace(name="Streaming checkpoint")
        provider = SiteModelProvider(key=f"stream-{uuid4()}", name="Test", model="test",
            connection_id=connection_id(db))
        db.add_all([workspace, provider]); db.flush()
        binding = LLMProviderConfig(workspace_id=workspace.id, site_provider_id=provider.id,
            key="test", name="test", model="test", base_url="", encrypted_api_key="")
        conversation = Conversation(workspace_id=workspace.id, title="Ordered")
        db.add_all([binding, conversation]); db.flush()
        blocks = [{"id": "text-1", "type": "text", "text": "first"},
            {"id": "activity-one", "type": "activity", "call_id": "one"},
            {"id": "text-2", "type": "text", "text": "second"}]
        message = ChatMessage(conversation_id=conversation.id, sequence=0, role="assistant",
            content="first\n\nsecond", display_blocks=blocks)
        db.add(message)
        for through in (1, 3):
            db.add(ContextCheckpoint(conversation_id=conversation.id, request_id=request_id,
                through_sequence=through, source_digest="a"*64, scope_digest="b"*64, summary="safe summary",
                provider_id=binding.id, model="test", estimated_before=2000, estimated_after=500))
        db.commit()
        identifier = message.id
    with Session(auth_postgres) as db:
        assert db.get(ChatMessage, identifier).display_blocks == blocks
        assert len(db.scalars(select(ContextCheckpoint).where(ContextCheckpoint.request_id == request_id)).all()) == 2
