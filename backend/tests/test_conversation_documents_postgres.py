import json
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import auth, conversation_documents as saves, storage
from app.models import ChatMessage, Conversation, ConversationDocumentSave, Document, KnowledgeBase
from test_auth_postgres import auth_postgres


def test_parallel_save_has_one_document_receipt_and_dispatch(auth_postgres, monkeypatch):
    factory = sessionmaker(bind=auth_postgres, expire_on_commit=False)
    with factory() as db:
        user = auth.create_user(db, f'output-{uuid4().hex}@example.com', 'Output test')
        workspace = user.id
        login = auth.login_response(db,user,action='auth.test')
        cookie=SimpleCookie(); cookie.load(login.headers['set-cookie'])
        access=auth.Access(user.id,auth.digest(cookie['rag_session'].value))
        library=KnowledgeBase(account_id=workspace, owner_id=user.id, name='Output tests')
        conversation=Conversation(account_id=workspace,owner_id=user.id,title='Synthetic')
        db.add_all([library,conversation]); db.flush()
        rid=uuid4()
        message=ChatMessage(conversation_id=conversation.id,sequence=1,role='assistant',request_id=rid)
        db.add(message); db.commit()
        runtime=SimpleNamespace(factory=factory,access=access,message_id=message.id,request_id=rid,verified_citations=[])
        library_id=library.id
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    stored, queued = store.objects, []
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    monkeypatch.setattr(saves,'dispatch_ingestion',queued.append)
    barrier=Barrier(3)
    def save(_):
        barrier.wait(timeout=10)
        return saves.execute(runtime,'save_to_knowledge_base',{'knowledge_base_id':str(library_id),'title':'Synthetic','markdown':'# Synthetic\nConfirmed output.'})
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(save,range(3)))
    assert len({r['save_id'] for r in results})==1
    assert len({r['document_id'] for r in results})==1
    assert len(stored)==len(queued)==1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Document).where(Document.knowledge_base_id==library_id))==1
        assert db.scalar(select(func.count()).select_from(ConversationDocumentSave).where(ConversationDocumentSave.knowledge_base_id==library_id))==1
