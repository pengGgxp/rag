"""对话与消息的共享读取和序列化。"""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .. import resource_mentions as resource_references
from ..identity.auth import Access
from ..models import ChatMessage, ContextCheckpoint, Conversation, Document, DocumentStatus, KnowledgeBaseStatus
from .attachments import attachment_read
from .feedback import feedback_read


def resolve_input_mentions(db, access, document_ids, knowledge_base_ids, resource_ids, *, skip_unavailable=False):
    documents, libraries, resources = map(lambda ids: list(dict.fromkeys(ids)),
        (document_ids, knowledge_base_ids, resource_ids))
    if len(documents) + len(libraries) + len(resources) > 8:
        raise HTTPException(422, "每条消息最多可以提及 8 项资料")

    def ready_library(identifier):
        library = access.library(db, identifier)
        if library.status == KnowledgeBaseStatus.ARCHIVED:
            raise HTTPException(404, "知识库已归档")
        return library

    knowledge_base_mentions = []
    for identifier in libraries:
        try:
            library = ready_library(identifier)
        except HTTPException:
            if not skip_unavailable:
                raise HTTPException(409, "提及的知识库已不可用，请重新选择") from None
            continue
        knowledge_base_mentions.append({"id": str(library.id), "name": library.name})

    document_mentions = []
    rows = {row.id: row for row in db.scalars(select(Document).where(Document.id.in_(documents)))} if documents else {}
    for identifier in documents:
        row = rows.get(identifier)
        try:
            if row is None or row.status != DocumentStatus.READY or not row.is_active:
                raise HTTPException(404, "文档不可用")
            library = ready_library(row.knowledge_base_id)
        except HTTPException:
            if not skip_unavailable:
                raise HTTPException(409, "提及的文档已不可用，请重新选择") from None
            continue
        document_mentions.append({"id": str(row.id), "filename": row.filename,
            "knowledge_base_id": str(library.id), "knowledge_base_name": library.name})
    resource_mentions = resource_references.resolve(db, access, resources, skip_unavailable=skip_unavailable)
    return {"document_mentions": document_mentions, "knowledge_base_mentions": knowledge_base_mentions,
        "resource_mentions": resource_mentions}


def message_read(message: ChatMessage) -> dict:
    return {
        "id": str(message.id), "sequence": message.sequence, "role": message.role,
        "content": message.content, "status": message.status,
        "document_mentions": message.document_mentions or [],
        "knowledge_base_mentions": message.knowledge_base_mentions or [],
        "resource_mentions": message.resource_mentions or [],
        "attachments": [attachment_read(row) for row in message.attachments],
        "request_id": str(message.request_id) if message.request_id else None,
        "provider_id": str(message.provider_id) if message.provider_id else None,
        "site_provider_id": str(message.provider.site_provider_id)
            if message.provider and message.provider.site_provider_id else None,
        "provider_name": message.provider_name, "model": message.model,
        "thinking": message.thinking_snapshot,
        "thinking_diagnostics": message.thinking_diagnostics,
        "citations": message.citations, "web_sources": message.web_sources, "traces": message.traces,
        "tool_events": message.tool_events,
        "display_blocks": message.display_blocks,
        "refused": message.refused, "refusal_reason": message.refusal_reason,
        "retrieval_query": message.retrieval_query, "error_detail": message.error_detail,
        "feedback": feedback_read(message.feedback),
        "created_at": message.created_at.isoformat(),
    }


def conversation_read(conversation: Conversation, detail=False) -> dict:
    data = {
        "id": str(conversation.id), "title": conversation.title,
        "title_source": conversation.title_source, "revision": conversation.revision,
        "pinned_at": conversation.pinned_at.isoformat() if conversation.pinned_at else None,
        "knowledge_bases": [],
        "active_request_id": str(conversation.active_request_id) if conversation.active_request_id else None,
        "skill_ids": conversation.skill_ids if conversation.extensions_initialized else [],
        "mcp_server_ids": conversation.mcp_server_ids if conversation.extensions_initialized else [],
        "question_workbench": conversation.question_workbench,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }
    if detail:
        data["messages"] = [message_read(message) for message in conversation.messages]
        db = object_session(conversation)
        from ..models import ConversationTitleTask
        title_task = db.get(ConversationTitleTask, conversation.id)
        data["title_task_status"] = title_task.status if title_task else None
        data["compactions"] = [{"id": str(checkpoint.id),
            "after_sequence": checkpoint.through_sequence,
            "created_at": checkpoint.created_at.isoformat()}
            for checkpoint in db.scalars(select(ContextCheckpoint).where(
                ContextCheckpoint.conversation_id == conversation.id,
                ContextCheckpoint.through_sequence >= 0)
                .order_by(ContextCheckpoint.created_at, ContextCheckpoint.id))]
    return data


def live_conversation(db: Session, conversation_id: UUID, access: Access) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(404, "会话不存在或已删除")
    access.conversation(db, conversation)
    return conversation
