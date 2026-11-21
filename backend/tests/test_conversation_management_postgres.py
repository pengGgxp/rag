"""Durable chat claims and migration preservation on disposable PostgreSQL."""
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import conversation_jobs, conversations
from app.models import (ChatMessage, Conversation, ConversationEvent, ConversationExecution,
    ConversationTitleTask, UserCreditAccount, CreditLedgerEntry, User)
from legacy_migration_helpers import legacy_tables, seed_workspace
from test_auth_postgres import auth_postgres


def test_concurrent_workers_claim_once_and_event_task_constraints(auth_postgres, monkeypatch):
    factory = sessionmaker(bind=auth_postgres, expire_on_commit=False)
    identifier = uuid4()
    with factory() as db:
        user = User(email=f"worker-{uuid4().hex}@example.com", name="Chat worker validation")
        db.add(user); db.flush()
        conversation = Conversation(account_id=user.id, owner_id=user.id, title="历史对话", title_source="legacy")
        db.add(conversation); db.flush()
        message = ChatMessage(conversation_id=conversation.id, sequence=1, role="assistant", status="streaming", request_id=identifier)
        db.add(message); db.flush()
        db.add(ConversationExecution(request_id=identifier, conversation_id=conversation.id,
            message_id=message.id, encrypted_payload="mock-only"))
        conversation_id, message_id = conversation.id, message.id
        db.commit()
    calls = []
    monkeypatch.setattr(conversation_jobs, "unpack", lambda _: (None, "0", None, None))
    async def generate(*args, **kwargs):
        calls.append(identifier)
        with factory() as db:
            db.get(ChatMessage, message_id).status = "completed"
            db.commit()
        yield f'id: {identifier}:1\nevent: done\ndata: {{"status":"completed"}}\n\n'
    monkeypatch.setattr(conversations, "generate", generate)
    barrier = Barrier(4)
    def run(_):
        barrier.wait(timeout=10)
        conversation_jobs.run(identifier, factory, retrieve=lambda: None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(4)))
    assert calls == [identifier]
    with factory() as db:
        assert db.get(ConversationExecution, identifier).status == "completed"
        assert len(list(db.scalars(select(ConversationEvent).where(ConversationEvent.request_id == identifier)))) == 1
        db.add(ConversationEvent(request_id=identifier, sequence=1, encrypted_payload="duplicate"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(ConversationTitleTask(conversation_id=conversation_id, request_id=identifier))
        db.commit()
        db.add(ConversationTitleTask(conversation_id=conversation_id, request_id=identifier))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_upgrade_keeps_legacy_titles_messages_and_money(auth_postgres):
    url = make_url(os.environ["AUTH_TEST_DATABASE_URL"])
    name = f"rag_auth_test_chat_migration_{uuid4().hex[:10]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database_url, "MIGRATION_DATABASE_URL": database_url}
    engine = create_engine(env["DATABASE_URL"])
    root = Path(__file__).resolve().parents[1]
    def migrate(target):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root, env=env,
            capture_output=True, check=True, timeout=60)
    try:
        migrate("0061_resource_previews")
        conversation_id = uuid4()
        with engine.begin() as db:
            user_id, workspace_id = seed_workspace(db)
            tables = legacy_tables(db, "credit_accounts", "credit_ledger")
            db.execute(tables["credit_accounts"].insert().values(workspace_id=workspace_id,
                balance=Decimal("18.2345"), lifetime_granted=Decimal("18.2345"), lifetime_spent=0))
            db.execute(tables["credit_ledger"].insert().values(id=uuid4(), workspace_id=workspace_id,
                actor_user_id=user_id, amount=Decimal("18.2345"), balance_after=Decimal("18.2345"),
                kind="grant", idempotency_key=f"chat-migration-{uuid4()}", metadata={}))
            db.execute(text("""INSERT INTO conversations (id,workspace_id,owner_id,title,skill_ids,mcp_server_ids,
                extensions_initialized,question_workbench,next_sequence) VALUES (:id,:workspace,:owner,
                '保留旧标题','[]','[]',true,false,0)"""), {"id": conversation_id, "workspace": workspace_id, "owner": user_id})
            # Seed the old schema without columns added by later migrations.
            db.execute(text("""INSERT INTO chat_messages (id,conversation_id,sequence,role,content,
                status,citations,traces,refused) VALUES (:id,:conversation,0,'user',:content,
                'completed','[]','[]',false)"""),
                {"id": uuid4(), "conversation": conversation_id, "content": "历史正文"})
        migrate("head")
        migrate("head")
        with Session(engine) as db:
            row = db.get(Conversation, conversation_id)
            assert row.title == "保留旧标题" and row.title_source == "legacy" and row.revision == 1 and row.pinned_at is None
            assert row.messages[0].content == "历史正文"
            assert row.messages[0].knowledge_base_mentions == []
            assert db.get(UserCreditAccount, user_id).balance == Decimal("18.2345")
            assert db.scalar(select(CreditLedgerEntry.amount).where(CreditLedgerEntry.account_id == user_id)) == Decimal("18.2345")
            assert not list(db.scalars(select(ConversationExecution)))
            assert not list(db.scalars(select(ConversationTitleTask)))
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
