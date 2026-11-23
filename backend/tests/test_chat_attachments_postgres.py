from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth
from app.conversations.attachments import select_attachments
from app.llm import ChatProviderConfig
from app.models import ChatAttachment, ChatMessage, Conversation, User
from test_auth_postgres import auth_postgres


def test_concurrent_binding_is_once_and_preserves_original(auth_postgres):
    with Session(auth_postgres) as db:
        user = User(email=f"attachment-{uuid4()}@example.com", name="Attachment test")
        db.add(user); db.flush()
        conversations = [Conversation(owner_id=user.id, account_id=user.id) for _ in range(2)]
        db.add_all(conversations); db.flush()
        row = ChatAttachment(account_id=user.id, user_id=user.id, filename="facts.txt",
            content_type="text/plain", size_bytes=5, content_hash="a" * 64, object_key=f"_chat/{user.id}/{uuid4()}.txt",
            kind="document", text_content="facts")
        db.add(row); db.commit()
        identifier, owner, ids = row.id, user.id, [item.id for item in conversations]
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    barrier = Barrier(2)
    def bind(cid):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                access = auth.Access(user_id=owner, session_hash="test-only")
                selected = select_attachments(db, access, [identifier], ChatProviderConfig("", "", "text"))
                db.add(ChatMessage(conversation_id=cid, role="user", sequence=0, attachments=selected))
                db.commit()
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(bind, ids)) == [200, 200]
    with Session(auth_postgres) as db:
        saved = db.get(ChatAttachment, identifier)
        assert saved.message_id is None and saved.content_hash == "a" * 64 and saved.text_content == "facts"
        messages = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id.in_(ids))).all()
        assert len(messages) == 2 and all(item.attachments[0].id == identifier for item in messages)
