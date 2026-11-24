import hashlib
import io
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import conversation_documents as saves, main, storage

from app.identity import auth
from app.knowledge.ingestion import admission as document_ingestion
from app.extensions import ExtensionRuntime
from app.models import (ChatMessage, ConversationDocumentSave, Document, DocumentStatus, IngestionJob,
    JobState, KnowledgeBase, KnowledgeBaseStatus, UsageEvent)
from test_auth import library, register
from test_retrieval import add_document


@pytest.fixture
def saving(accounts, monkeypatch):
    (owner, other, anonymous), db = accounts
    session = register(owner)
    library_id = library(owner)
    conversation = owner.post('/conversations', json={}).json()
    request_id = uuid4()
    message = ChatMessage(conversation_id=UUID(conversation['id']), sequence=2, role='assistant',
        request_id=request_id, content='Synthetic output', status='completed')
    db.add(message); db.commit()
    access = auth.Access(UUID(session['user']['id']), auth.digest(owner.cookies.get(auth.COOKIE)))
    from test_cloud_computer_mount import MemoryMount
    store = MemoryMount()
    stored, queued, deleted = store.objects, [], []
    monkeypatch.setattr("app.file_operations.storage", lambda _: store)
    monkeypatch.setattr(saves, 'dispatch_ingestion', queued.append)
    runtime = ExtensionRuntime(sessionmaker(bind=db.get_bind(), expire_on_commit=False), access,
        message_id=message.id, request_id=request_id)
    return SimpleNamespace(owner=owner, other=other, anonymous=anonymous, db=db, library=library_id,
        runtime=runtime, message=message, access=access, stored=stored, queued=queued, deleted=deleted)


def payload(env, **changes):
    return {'knowledge_base_id':str(env.library), 'title':'工作规范', 'markdown':'# 工作规范\n\n每周五核对资料。', **changes}


def test_save_uses_upload_pipeline_receipts_and_deduplication(saving):
    e = saving
    first = e.runtime.execute('save_to_knowledge_base', payload(e))
    assert first.get('save_id'), first
    assert first['document_status']=='queued' and not first['terminal']
    assert first['summary']=='已提交，正在处理' and 'document=' in first['url']
    again = e.runtime.execute('save_to_knowledge_base', payload(e))
    assert again['save_id']==first['save_id']
    assert len(e.stored)==len(e.queued)==1
    # A later user request still deduplicates through the library content hash.
    rid = uuid4()
    msg = ChatMessage(conversation_id=e.message.conversation_id, sequence=3, role='assistant', request_id=rid)
    e.db.add(msg); e.db.commit()
    e.runtime.request_id, e.runtime.message_id = rid, msg.id
    later = e.runtime.execute('save_to_knowledge_base', payload(e))
    assert later['document_id']==first['document_id'] and later['save_id']!=first['save_id']
    assert len(e.stored)==len(e.queued)==1
    assert e.db.scalar(select(func.count()).select_from(Document))==1
    assert e.db.scalar(select(func.count()).select_from(UsageEvent))==0
    doc = e.db.get(Document, UUID(first['document_id']))
    assert doc.content_origin=='ai_assisted' and doc.content_type=='text/markdown'
    preview = e.owner.get(f'/documents/{doc.id}/preview').json()
    assert preview['content_origin']=='ai_assisted'
    assert 'conversation_id' not in preview and 'message_id' not in preview


def test_scopes_writable_targets_roles_and_live_revocation(saving):
    e = saving
    other_session = register(e.other, 'outsider@example.com')
    other_library = library(e.other)
    available = e.runtime.execute('list_writable_knowledge_bases', {})['knowledge_bases']
    assert [v['id'] for v in available]==[str(e.library)]
    assert e.runtime.execute('save_to_knowledge_base',payload(e,knowledge_base_id=str(other_library))).get('error')
    # Saving to a writable library outside conversation retrieval scope is allowed.
    target = library(e.owner)
    good = e.runtime.execute('save_to_knowledge_base',payload(e,knowledge_base_id=str(target)))
    assert good.get('save_id'), good
    assert e.other.get('/conversation-document-saves/'+good['save_id']).status_code==404
    assert e.anonymous.get('/conversation-document-saves/'+good['save_id']).status_code==401
    assert e.owner.put('/auth/members', json={'email': 'outsider@example.com', 'role': 'viewer'}).status_code==404
    assert e.other.get(f"/documents/{good['document_id']}/preview").status_code==404
    assert e.other.get('/conversation-document-saves/'+good['save_id']).status_code==404
    fresh = ExtensionRuntime(e.runtime.factory,e.access)
    assert 'save_to_knowledge_base' in [v['function']['name'] for v in fresh.tools]


def test_validation_archival_and_status_never_restarts(saving):
    e = saving
    for changes in ({'markdown':'  '},{'title':'../bad'},{'title':' '},{'markdown':'\0'}, {'owner_id':str(uuid4())}):
        assert e.runtime.execute('save_to_knowledge_base', payload(e,**changes)).get('error')
    e.db.get(KnowledgeBase,e.library).status=KnowledgeBaseStatus.ARCHIVED; e.db.commit()
    assert e.runtime.execute('save_to_knowledge_base',payload(e)).get('error')
    e.db.get(KnowledgeBase,e.library).status=KnowledgeBaseStatus.READY; e.db.commit()
    result=e.runtime.execute('save_to_knowledge_base',payload(e))
    doc=e.db.get(Document,UUID(result['document_id'])); doc.status=DocumentStatus.READY; e.db.commit()
    response=e.owner.get('/conversation-document-saves/'+result['save_id']).json()
    assert response['terminal'] and response['summary']=='已存入，可供检索'
    doc.status=DocumentStatus.FAILED; e.db.commit()
    assert e.owner.get('/conversation-document-saves/'+result['save_id']).json()['terminal']
    doc.status=DocumentStatus.DELETED; e.db.commit()
    assert e.owner.get('/conversation-document-saves/'+result['save_id']).json()['document_status']=='deleted'
    assert len(e.queued)==1


def test_dispatch_failure_recovery_is_persistent(saving,monkeypatch):
    e=saving
    def unavailable(_): raise ConnectionError('synthetic queue unavailable')
    monkeypatch.setattr(saves,'dispatch_ingestion',unavailable)
    result=e.runtime.execute('save_to_knowledge_base',payload(e))
    assert result.get('save_id'),result
    receipt=e.db.get(ConversationDocumentSave,UUID(result['save_id']))
    assert receipt.dispatch_pending
    e.owner.get('/conversation-document-saves/'+result['save_id'])
    monkeypatch.setattr(saves,'dispatch_ingestion',e.queued.append)
    saves.recover_pending(e.runtime.factory)
    e.db.expire_all()
    assert not e.db.get(ConversationDocumentSave,receipt.id).dispatch_pending
    saves.recover_pending(e.runtime.factory)
    assert len(e.queued)==len(e.stored)==1


def test_verified_provenance_and_text_parser_do_not_invent_sources(saving):
    e=saving
    doc=add_document(e.db,e.db.get(KnowledgeBase,e.library),'Synthetic evidence')
    chunk=doc.chunks[0]
    e.runtime.verified_citations=[{'chunk_id':str(chunk.id)}, {'chunk_id':str(uuid4())}]
    result=e.runtime.execute('save_to_knowledge_base',payload(e,markdown=f'结论 [{chunk.id}]'))
    receipt=e.db.get(ConversationDocumentSave,UUID(result['save_id']))
    assert receipt.source_citations==[{'chunk_id':str(chunk.id),'document_id':str(doc.id),'original_hash':doc.content_hash,'page_number':None}]
    from app.knowledge.ingestion.pipeline import parse_for_ingestion
    created=e.db.get(Document,UUID(result['document_id']))
    parsed=parse_for_ingestion(e.db,e.db.get(IngestionJob,receipt.job_id),created,next(iter(e.stored.values())))
    assert str(chunk.id) in parsed.text and created.parsing_metadata is None


def test_commit_failure_cleans_object(saving,monkeypatch):
    e=saving
    def failed(*_, **__):
        raise RuntimeError('synthetic metadata failure')
    monkeypatch.setattr(document_ingestion, 'record_audit', failed)
    with pytest.raises(RuntimeError):
        saves.execute(e.runtime, 'save_to_knowledge_base', payload(e))
    assert not e.stored and not e.queued
    assert e.db.scalar(select(func.count()).select_from(Document)) == 0


def test_stream_emits_saved_document_activity_at_actual_call(saving):
    from app.agent.loop import stream_retrieval_agent
    from app.model_runtime.client import ToolCall, ToolResponse
    e=saving
    class Provider:
        step=0
        def complete_with_tools(self,messages,tools,tool_choice):
            self.step+=1
            if self.step==1:
                return ToolResponse(content=None,tool_calls=[ToolCall(id='save-output',name='save_to_knowledge_base',arguments=payload(e))])
            return ToolResponse(content='已提交，正在处理。',tool_calls=[])
    events=list(stream_retrieval_agent(query='请把工作规范保存到指定知识库。',knowledge_base_ids=[],history=[],
        factory=e.runtime.factory,access=e.access,retrieve=lambda *_: None,provider=Provider(),extensions=e.runtime))
    result=next(data for name,data in events if name=='tool_result')
    assert result['saved_document']['document_id']
    assert result['summary']=='已提交，正在处理'
    assert next(i for i,(name,_) in enumerate(events) if name=='tool_started') < next(i for i,(name,_) in enumerate(events) if name=='tool_result')
