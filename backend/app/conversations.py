import json
import logging
import time
import asyncio
from threading import Event
from datetime import datetime, timezone
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, sessionmaker
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .llm import ChatProviderConfig, LLMConfigurationError, LLMProviderError, OpenAICompatibleChatProvider
from .agent import AgentCancelled, stream_retrieval_agent
from .auth import Access, get_access
from .models import (
    ChatMessage,
    Conversation,
    Document,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from .feedback import feedback_read
from .audit_log import record_audit
from .credits import (
    CreditError, authorization_hold, authorization_hold_amount,
    release_hold, settle_model_usage,
)
from .extensions import ExtensionRuntime, validate_selection, defaults as extension_defaults
from .llm_providers import request_provider_config
from . import model_billing
from . import resource_mentions as resource_references
from .models import ModelUsageRequest
from .thinking import encrypt_continuation, encrypt_display_thinking, decrypt_display_thinking
from .conversation_stream import events as stream_events
from .conversation_context import ContextManager, build_messages, HISTORY_TOOL
from .models import ContextCheckpoint, ContextOperation, SiteModelProvider, ThinkingTest
from .chat_attachments import attachment_read, select_attachments, user_content


class ExtensionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_ids: list[UUID] = Field(default_factory=list)
    mcp_server_ids: list[UUID] = Field(default_factory=list)
    question_workbench: bool = False


class ConversationCreate(ExtensionSelection):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(default="新对话", min_length=1, max_length=200)


class ConversationRename(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    revision: int = Field(ge=1)


class ContextRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    request_id: UUID
    provider_id: UUID
    thinking_option_id: str | None = Field(default=None, min_length=1, max_length=40)
    thinking_config_version: int | None = Field(default=None, ge=0, strict=True)


class InputReferences(BaseModel):
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=6)
    document_ids: list[UUID] = Field(default_factory=list, max_length=8)
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=8)
    resource_ids: list[UUID] = Field(default_factory=list, max_length=8)


class TurnRequest(ContextRequest, InputReferences):
    web_search: bool = True
    query: str | None = Field(default=None, min_length=1)
    retry_message_id: UUID | None = None

    @model_validator(mode="after")
    def one_operation(self):
        if bool(self.query or self.attachment_ids) == bool(self.retry_message_id):
            raise ValueError("Provide either query or retry_message_id")
        return self


class StopRequest(BaseModel):
    request_id: UUID


class SteerRequest(InputReferences):
    model_config = ConfigDict(str_strip_whitespace=True)
    message_id: UUID
    text: str = Field(default="", max_length=100_000)

    @model_validator(mode="after")
    def has_input(self):
        if not self.text and not self.attachment_ids:
            raise ValueError("Provide text or an attachment")
        return self


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
        from .models import ConversationTitleTask
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


def recover_interrupted_conversations(db: Session):
    from .models import RemoteToolExecution, ConversationExecution
    owned = select(ConversationExecution.request_id).where(ConversationExecution.status.in_(["queued", "running"]))
    db.execute(update(RemoteToolExecution).where(RemoteToolExecution.status == "running",
        ~RemoteToolExecution.request_id.in_(owned)).values(status="uncertain"))
    model_billing.recover_requests(db)
    for test in db.scalars(select(ThinkingTest).where(ThinkingTest.status == "running")):
        test.status = "interrupted"
        test.result = {"request_id": str(test.request_id), "version": test.version,
            "thinking_version": test.thinking_version, "completed": False,
            "error": "服务重启，测试已中断，已确认用量按原规则结算"}
    from .models import BillingSnapshot, CreditLedgerEntry
    from .workspace_billing import legacy_call_usage
    for snapshot in db.scalars(select(BillingSnapshot).where(
            BillingSnapshot.event_key == "model", BillingSnapshot.status == "running",
            ~BillingSnapshot.request_id.in_(owned))):
        hold = db.scalar(select(CreditLedgerEntry).where(
            CreditLedgerEntry.idempotency_key == f"usage:{snapshot.request_id}:hold"))
        if hold:
            settle_model_usage(db, snapshot.workspace_id, hold.actor_user_id, snapshot.request_id,
                legacy_call_usage(db, snapshot.workspace_id, snapshot.request_id), -hold.amount)
        snapshot.status = "interrupted"
    for message in db.scalars(select(ChatMessage).where(ChatMessage.status == "streaming", ~ChatMessage.request_id.in_(owned))):
        message.status = "interrupted"
        message.error_detail = "服务重启，回答已中断，可以重试"
        message.tool_events = [
            {**item, "status": "interrupted"} if item.get("status") in ("waiting", "running") else item
            for item in message.tool_events
        ]
        message.display_blocks = [
            {**block, "status": "interrupted"} if block.get("type") == "thinking" and block.get("status") == "running"
            else block for block in message.display_blocks
        ]
    db.execute(update(Conversation).where(Conversation.active_request_id.is_not(None), ~Conversation.active_request_id.in_(owned)).values(
        active_request_id=None,
    ))
    db.execute(update(ContextOperation).where(ContextOperation.status == "running", ~ContextOperation.request_id.in_(owned)).values(
        status="interrupted", result={"status": "failed", "error": "服务重启，整理已中断，原始记录已保留"}))
    db.commit()


def event(name, data):
    return {"event": name, "data": json.dumps(data, ensure_ascii=False)}


def event_text(payload):
    prefix = f"id: {payload['id']}\n" if payload.get("id") else ""
    return f"{prefix}event: {payload['event']}\ndata: {payload['data']}\n\n"


def chat_router(get_db, retrieve):
    router = APIRouter(prefix="/conversations", tags=["conversations"])

    @router.get("/{conversation_id}/messages/{message_id}/thinking")
    def read_thinking(conversation_id: UUID, message_id: UUID,
            db: Session = Depends(get_db), access: Access = Depends(get_access)):
        live_conversation(db, conversation_id, access)
        message = db.get(ChatMessage, message_id)
        if message is None or message.conversation_id != conversation_id or message.role != "assistant":
            raise HTTPException(404, "回答不存在")
        try:
            return {"blocks": decrypt_display_thinking(message.encrypted_display_thinking)}
        except Exception:
            raise HTTPException(503, "思考内容暂时无法读取，请稍后重试") from None

    @router.post("", status_code=201)
    def create(payload: ConversationCreate, request: Request,
               db: Session = Depends(get_db), access: Access = Depends(get_access)):
        if not ({"skill_ids", "mcp_server_ids"} & payload.model_fields_set):
            skill_ids, server_ids = extension_defaults(db, access)
            payload.skill_ids, payload.mcp_server_ids = list(map(UUID, skill_ids)), list(map(UUID, server_ids))
        validate_selection(db, access, payload.skill_ids, payload.mcp_server_ids)
        if payload.question_workbench:
            from .question_workbench import status as workbench_status
            workbench = workbench_status(db, access)
            if not (workbench["available"] and workbench["enabled"]):
                raise HTTPException(403, "请先授权题目工作台")
        conversation = Conversation(
            title=payload.title, title_source="temporary" if payload.title == "新对话" else "manual",
            owner_id=access.user_id, workspace_id=access.workspace_id, extensions_initialized=True,
            skill_ids=list(dict.fromkeys(map(str, payload.skill_ids))),
            question_workbench=payload.question_workbench,
            mcp_server_ids=list(dict.fromkeys(map(str, payload.mcp_server_ids))),
        )
        db.add(conversation)
        db.flush()
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="conversation.created", resource_type="conversation",
            resource_id=conversation.id, metadata={"knowledge_base_count": 0}, request=request,
        )
        db.commit()
        db.refresh(conversation)
        return conversation_read(conversation, True)

    @router.get("")
    def listing(cursor: str | None = None, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .conversation_management import page
        return page(db, access, cursor, conversation_read)

    @router.get("/search")
    def search(q: str = "", cursor: str | None = None, since: datetime | None = None,
               until: datetime | None = None, knowledge_base_id: UUID | None = None,
               has_attachments: bool = False, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .conversation_management import search_page
        return search_page(db, access, q, cursor, since, until, knowledge_base_id, has_attachments)

    @router.get("/knowledge-catalog")
    def knowledge_catalog(q: str = "", db: Session = Depends(get_db), access: Access = Depends(get_access)):
        query = q.strip()
        if len(query) > 120:
            raise HTTPException(422, "搜索内容不能超过 120 个字符")
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        library_filter = [
            access.visible_libraries(),
            KnowledgeBase.status != KnowledgeBaseStatus.ARCHIVED,
        ]
        document_filter = [
            access.visible_libraries(),
            KnowledgeBase.status != KnowledgeBaseStatus.ARCHIVED,
            Document.status == DocumentStatus.READY,
            Document.is_active.is_(True),
        ]
        if query:
            library_filter.append(or_(
                KnowledgeBase.name.ilike(pattern, escape="\\"),
                KnowledgeBase.description.ilike(pattern, escape="\\"),
            ))
            document_filter.append(or_(
                Document.filename.ilike(pattern, escape="\\"),
                KnowledgeBase.name.ilike(pattern, escape="\\"),
            ))
        libraries = db.scalars(select(KnowledgeBase).where(*library_filter)
            .order_by(KnowledgeBase.name, KnowledgeBase.id).limit(20)).all()
        documents = db.execute(select(Document, KnowledgeBase)
            .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
            .where(*document_filter)
            .order_by(Document.updated_at.desc(), Document.id).limit(20)).all()
        return {
            "knowledge_bases": [{"id": str(row.id), "name": row.name} for row in libraries],
            "documents": [{
                "id": str(document.id), "filename": document.filename,
                "knowledge_base_id": str(document.knowledge_base_id),
                "knowledge_base_name": library.name,
            } for document, library in documents],
            "resources": resource_references.catalog(db, access, query),
        }

    @router.get("/{conversation_id}")
    def detail(conversation_id: UUID, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        return conversation_read(live_conversation(db, conversation_id, access), True)

    @router.get("/{conversation_id}/requests/{request_id}")
    def execution_status(conversation_id: UUID, request_id: UUID,
                         db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .models import ConversationExecution
        from .conversation_jobs import public_status
        live_conversation(db, conversation_id, access)
        row = db.get(ConversationExecution, request_id)
        if row is None or row.conversation_id != conversation_id:
            raise HTTPException(404, "回答不存在")
        return public_status(row)

    @router.get("/{conversation_id}/requests/{request_id}/events")
    def execution_events(conversation_id: UUID, request_id: UUID, after: int = 0,
                         db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .models import ConversationExecution
        from .conversation_jobs import subscribe
        live_conversation(db, conversation_id, access)
        row = db.get(ConversationExecution, request_id)
        if row is None or row.conversation_id != conversation_id or after < 0:
            raise HTTPException(404, "回答不存在")
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        return StreamingResponse(subscribe(factory, access, conversation_id, request_id, after),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.put("/{conversation_id}/extensions")
    def extensions(conversation_id: UUID, payload: ExtensionSelection,
                   db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
        if conversation is None or conversation.deleted_at is not None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        if conversation.active_request_id:
            raise HTTPException(409, "请等待当前回答完成后再调整扩展")
        validate_selection(db, access, payload.skill_ids, payload.mcp_server_ids)
        conversation.skill_ids = list(dict.fromkeys(map(str, payload.skill_ids)))
        conversation.mcp_server_ids = list(dict.fromkeys(map(str, payload.mcp_server_ids)))
        if payload.question_workbench:
            from .question_workbench import status as workbench_status
            workbench = workbench_status(db, access)
            if not (workbench["available"] and workbench["enabled"]):
                raise HTTPException(403, "请先授权题目工作台")
        conversation.question_workbench = payload.question_workbench
        conversation.extensions_initialized = True
        db.commit()
        return conversation_read(conversation)
    @router.patch("/{conversation_id}")
    def rename(conversation_id: UUID, payload: ConversationRename, request: Request,
               db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
        if conversation is None or conversation.deleted_at is not None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        if conversation.revision != payload.revision:
            raise HTTPException(409, "对话已更新，请刷新后重试；输入的标题仍保留")
        activity = conversation.updated_at
        if payload.title is not None:
            conversation.title = payload.title
            conversation.title_source = "manual"
        if payload.pinned is not None:
            conversation.pinned_at = datetime.now(timezone.utc) if payload.pinned else None
        conversation.revision += 1
        conversation.updated_at = activity
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(conversation, "updated_at")
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="conversation.renamed", resource_type="conversation",
            resource_id=conversation.id, metadata={"pinned": payload.pinned}, request=request,
        )
        db.commit()
        return conversation_read(conversation)

    @router.delete("/{conversation_id}", status_code=204)
    def delete(conversation_id: UUID, request: Request,
               db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.deleted_at.is_(None),
        ).with_for_update())
        if conversation is None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        db.execute(update(ChatMessage).where(
            ChatMessage.conversation_id == conversation.id, ChatMessage.status == "streaming",
        ).values(status="cancelled", error_detail=None))
        conversation.deleted_at = datetime.now(timezone.utc)
        conversation.active_request_id = None
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="conversation.deleted", resource_type="conversation",
            resource_id=conversation.id, request=request,
        )
        db.commit()
        return Response(status_code=204)

    @router.get("/{conversation_id}/requests/{request_id}/tools/{execution_id}")
    def tool_detail(conversation_id: UUID, request_id: UUID, execution_id: UUID, offset: int = Query(0, ge=0),
                    db: Session = Depends(get_db), access: Access = Depends(get_access)):
        access = access.revalidate(db)
        live_conversation(db, conversation_id, access)
        from .tool_detail import receipt_projection
        return JSONResponse(receipt_projection(db, access, conversation_id, request_id, execution_id, offset),
                            headers={"Cache-Control": "private, no-store"})

    @router.post("/{conversation_id}/requests/{request_id}/steer")
    def steer(conversation_id: UUID, request_id: UUID, payload: SteerRequest,
              db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .conversation_jobs import update_steering
        conversation = live_conversation(db, conversation_id, access.revalidate(db))
        return update_steering(db, conversation, request_id, payload.message_id,
            payload.model_dump(mode="json", exclude={"message_id"}, exclude_defaults=True))

    @router.delete("/{conversation_id}/requests/{request_id}/steer/{message_id}")
    def withdraw_steering(conversation_id: UUID, request_id: UUID, message_id: UUID,
                          db: Session = Depends(get_db), access: Access = Depends(get_access)):
        from .conversation_jobs import update_steering
        conversation = live_conversation(db, conversation_id, access.revalidate(db))
        return update_steering(db, conversation, request_id, message_id, None)

    @router.post("/{conversation_id}/stop")
    def stop(conversation_id: UUID, payload: StopRequest, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.deleted_at.is_(None),
        ).with_for_update())
        if conversation is None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        message = db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == conversation.id, ChatMessage.request_id == payload.request_id,
        ))
        operation = db.get(ContextOperation, payload.request_id)
        if message is None and (operation is None or operation.conversation_id != conversation_id):
            raise HTTPException(404, "回答不存在")
        if message:
            from .conversation_jobs import recover_steered_blocks
            db.execute(update(ChatMessage).where(
                ChatMessage.id == message.id, ChatMessage.status == "streaming",
            ).values(status="cancelled", error_detail=None, display_blocks=recover_steered_blocks(
                db, payload.request_id, message.display_blocks)))
        elif operation.status == "running":
            operation.status = "cancelled"
            operation.result = {"status": "failed", "error": "整理已停止，原始记录已保留"}
        db.execute(update(Conversation).where(
            Conversation.id == conversation.id, Conversation.active_request_id == payload.request_id,
        ).values(active_request_id=None))
        from .agent_tool_jobs import cancel_request
        cancel_request(db, payload.request_id)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "请求已被提交，请刷新对话后重试") from exc
        if message:
            db.refresh(message)
            return message_read(message)
        return {**operation.result, "request_id": str(payload.request_id)}

    @router.get("/{conversation_id}/export")
    def export(conversation_id: UUID, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = live_conversation(db, conversation_id, access)
        lines = [
            f"# {conversation.title}", "",
        ]
        for message in conversation.messages:
            lines.extend([f"## {'你' if message.role == 'user' else '知库'}", "", message.content or "（未生成正文）", ""])
            lines.extend(f"附件：{row.filename}\n" for row in message.attachments)
            if message.role == "assistant":
                if message.status != "completed":
                    lines.extend([f"> 回答状态：{message.status}", ""])
                if message.refused:
                    lines.extend(["> 证据不足，未提供确定性答案", ""])
                for citation in message.citations:
                    lines.extend([
                        f"来源：{citation['filename']}（{citation['knowledge_base_name']}，"
                        f"v{citation.get('version_number', 1)}，字符 {citation['char_start']}–{citation['char_end']}，"
                        f"{'第 ' + str(citation['page_number']) + ' 页，' if citation.get('page_number') else ''}"
                        f"{citation['chunk_id']}）",
                        "", citation["text"], "",
                    ])
                for source in message.web_sources:
                    lines.extend([f"网页来源：{source['title']} — {source['url']}", ""])
        return Response(
            "\n".join(lines), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="conversation-{conversation.id}.md"'},
        )

    @router.get("/{conversation_id}/context")
    def context(conversation_id: UUID, provider_id: UUID, thinking_option_id: str | None = None,
                thinking_config_version: int | None = None, web_search: bool = True,
                db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = live_conversation(db, conversation_id, access)
        from .shared_providers import unavailable_reason
        from .llm import ChatProviderConfig
        from .thinking import select_thinking
        shared = db.get(SiteModelProvider, provider_id)
        if shared is None or unavailable_reason(db, shared, None, True):
            raise HTTPException(409, "当前模型不可用，请重新选择")
        from .model_connections import connection_for
        connection = connection_for(db, shared)
        config = ChatProviderConfig("", "", shared.model, site_provider_id=str(shared.id),
            connection_id=str(connection.id), api_protocol=shared.chat_protocol,
            supports_images=shared.supports_images,
            context_capacity=shared.context_capacity, max_output_tokens=shared.max_output_tokens,
            thinking=select_thinking(shared.thinking_config, shared.thinking_version,
                thinking_option_id, thinking_config_version))
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        runtime = ExtensionRuntime(factory, access,
            conversation.skill_ids if conversation.extensions_initialized else [],
            conversation.mcp_server_ids if conversation.extensions_initialized else [],
            question_workbench=conversation.question_workbench, provider_config=config)
        if not config.thinking.supports_tools:
            runtime.skills, runtime.tools = [], []
        manager = ContextManager(factory, access, conversation_id, config, None, None, None,
            skill_versions=runtime.versions if config.thinking.supports_tools else [])
        history = manager.history(db, conversation)
        db.commit()
        messages, tools = build_messages(db, conversation, [], config, runtime, history=history)
        messages.extend(history)
        if web_search and config.thinking.supports_tools and model_billing.enabled(db):
            from .search_settings import runtime_config
            from .web_search import tools as search_tools
            if runtime_config(db):
                tools += search_tools()
        return manager.state(messages, tools + ([HISTORY_TOOL] if config.thinking.supports_tools else []))

    @router.post("/{conversation_id}/compact")
    def compact(conversation_id: UUID, payload: ContextRequest,
                db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.get(Conversation, conversation_id, with_for_update=True)
        if conversation is None or conversation.deleted_at:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        existing = db.get(ContextOperation, payload.request_id)
        if db.get(ThinkingTest, payload.request_id):
            raise HTTPException(409, "请求标识已被使用，请重新发起")
        if existing:
            if existing.conversation_id != conversation_id:
                raise HTTPException(409, "整理请求正在执行或已被使用")
            from .models import ConversationExecution
            from .conversation_jobs import public_status
            execution = db.get(ConversationExecution, payload.request_id)
            if execution:
                return public_status(execution)
            async def repeat():
                yield event_text(event("done", existing.result))
            return StreamingResponse(repeat(), media_type="text/event-stream")
        if conversation.active_request_id:
            raise HTTPException(409, "请先停止或等待当前操作完成")
        config = request_provider_config(db, access.workspace_id, payload.provider_id,
            payload.thinking_option_id, payload.thinking_config_version)
        if not config.context_capacity or not config.max_output_tokens:
            raise HTTPException(409, "上下文保护未就绪，请联系管理员配置模型容量")
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        runtime = ExtensionRuntime(factory, access,
            conversation.skill_ids if conversation.extensions_initialized else [],
            conversation.mcp_server_ids if conversation.extensions_initialized else [],
            question_workbench=conversation.question_workbench, provider_config=config)
        manager = ContextManager(factory, access, conversation_id, config, None, None, payload.request_id, manual=True,
            skill_versions=runtime.versions if config.thinking.supports_tools else [])
        history = manager.history(db, conversation)
        from .chat_attachments import validate_messages
        validate_messages(history, config)
        # A checkpoint already covering the eligible prefix makes this a free no-op.
        if not manager.eligible():
            async def nothing():
                yield event_text(event("done", {"status": "idle", "eligible": False, "no_op": True}))
            db.commit()
            return StreamingResponse(nothing(), media_type="text/event-stream")
        hold = authorization_hold_amount()
        if model_billing.enabled(db):
            model_billing.start_request(db, access.workspace_id, access.user_id, payload.request_id,
                UUID(config.provider_id), hold)
        else:
            from .workspace_billing import operation_snapshot
            model_billing.settle_pending(db, access.workspace_id, hold)
            operation_snapshot(db, access.workspace_id, payload.request_id, "model",
                capabilities=model_billing.CAPABILITIES, provider_id=UUID(config.provider_id))
            authorization_hold(db, access.workspace_id, access.user_id, payload.request_id, hold)
        conversation.active_request_id = payload.request_id
        db.add(ContextOperation(request_id=payload.request_id, conversation_id=conversation_id))
        from .conversation_jobs import enqueue, dispatch, public_status
        execution = enqueue(db, conversation_id, None, payload.request_id, access, hold, config)
        db.commit()
        result = public_status(execution)
        dispatch(payload.request_id)
        return result

    @router.post("/{conversation_id}/messages")
    def send(conversation_id: UUID, payload: TurnRequest, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.deleted_at.is_(None),
        ).with_for_update())
        if conversation is None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        from .models import ConversationExecution
        from .conversation_jobs import public_status
        execution = db.get(ConversationExecution, payload.request_id)
        if execution:
            if execution.conversation_id != conversation.id:
                raise HTTPException(409, "该请求正在处理或已被使用")
            return public_status(execution)
        if db.get(ThinkingTest, payload.request_id):
            raise HTTPException(409, "请求标识已被使用，请重新发起")
        existing = db.scalar(select(ChatMessage).where(ChatMessage.request_id == payload.request_id))
        if existing:
            if existing.conversation_id != conversation.id:
                raise HTTPException(409, "该请求正在处理或已被使用")
            from .models import ConversationExecution
            from .conversation_jobs import public_status
            execution = db.get(ConversationExecution, payload.request_id)
            if execution:
                return public_status(execution)
            async def duplicate():
                yield event_text(event("done", message_read(existing)))
            return StreamingResponse(duplicate(), media_type="text/event-stream")
        retry = None
        previous_user = None
        document_mentions = []
        knowledge_base_mentions = []
        resource_mentions = []
        if payload.retry_message_id:
            retry = db.get(ChatMessage, payload.retry_message_id)
            latest = db.scalar(select(ChatMessage).where(
                ChatMessage.conversation_id == conversation.id,
            ).order_by(ChatMessage.sequence.desc()).limit(1))
            if (retry is None or retry.conversation_id != conversation.id or latest is None or latest.id != retry.id
                    or retry.role != "assistant" or retry.status not in ("failed", "cancelled", "interrupted")):
                raise HTTPException(409, "只能重试最后一条未完成的回答")
            previous_user = db.scalar(select(ChatMessage).where(
                ChatMessage.conversation_id == conversation.id,
                ChatMessage.sequence == retry.sequence - 1,
                ChatMessage.role == "user",
            ))
            document_mentions = list(previous_user.document_mentions or []) if previous_user else []
            knowledge_base_mentions = list(previous_user.knowledge_base_mentions or []) if previous_user else []
            resource_mentions = list(previous_user.resource_mentions or []) if previous_user else []
        mention_ids = ([UUID(item["id"]) for item in document_mentions] if retry
            else list(dict.fromkeys(payload.document_ids)))
        knowledge_base_mention_ids = ([UUID(item["id"]) for item in knowledge_base_mentions] if retry
            else list(dict.fromkeys(payload.knowledge_base_ids)))
        resource_ids = ([UUID(item["id"]) for item in resource_mentions] if retry
            else list(dict.fromkeys(payload.resource_ids)))
        checked_access = access.revalidate(db)
        current_mentions = resolve_input_mentions(db, checked_access, mention_ids, knowledge_base_mention_ids,
            resource_ids, skip_unavailable=bool(retry))
        saved_mentions = ({"document_mentions": document_mentions, "knowledge_base_mentions": knowledge_base_mentions,
            "resource_mentions": resource_mentions} if retry else current_mentions)
        try:
            hold_amount = authorization_hold_amount()
            metered = model_billing.enabled(db)
            search_config = None
            provider_config = request_provider_config(db, access.workspace_id, payload.provider_id,
                payload.thinking_option_id, payload.thinking_config_version)
            if not provider_config.thinking.supports_tools and any(current_mentions.values()):
                raise HTTPException(422, "当前模型无法使用资料引用，请选择支持工具的模型")
            attachments = select_attachments(db, access, [row.id for row in previous_user.attachments]
                if previous_user else payload.attachment_ids, provider_config)
            # Check retained multimodal history before reserving credits, including retries.
            from .chat_attachments import validate_messages
            factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
            manager = ContextManager(factory, access, conversation.id, provider_config, None, None, None)
            validate_messages(manager.history(db, conversation), provider_config)
            if metered:
                if payload.web_search and provider_config.thinking.supports_tools:
                    from .search_settings import runtime_config
                    search_config = runtime_config(db)
                model_billing.start_request(db, access.workspace_id, access.user_id,
                    payload.request_id, UUID(provider_config.provider_id), hold_amount, search_config=search_config)
            else:
                from .workspace_billing import operation_snapshot
                model_billing.settle_pending(db, access.workspace_id, hold_amount)
                operation_snapshot(db, access.workspace_id, payload.request_id, "model",
                    capabilities=model_billing.CAPABILITIES, provider_id=UUID(provider_config.provider_id))
                authorization_hold(db, access.workspace_id, access.user_id, payload.request_id, hold_amount)
        except CreditError:
            db.rollback()
            raise
        next_sequence = db.scalar(update(Conversation).where(
            Conversation.id == conversation.id, Conversation.deleted_at.is_(None),
            Conversation.active_request_id.is_(None),
        ).values(
            active_request_id=payload.request_id,
            next_sequence=Conversation.next_sequence + 2,
            updated_at=datetime.now(timezone.utc),
        ).returning(Conversation.next_sequence))
        if next_sequence is None:
            db.rollback()
            raise HTTPException(409, "当前会话正在回答，请先停止或等待完成")
        query = previous_user.content if previous_user else payload.query or ""
        user_message = ChatMessage(
            conversation_id=conversation.id, sequence=next_sequence - 2,
            role="user", content=query, attachments=attachments,
            **saved_mentions,
        )
        db.add(user_message)
        db.flush()
        from .models import chat_message_resources
        for position, attachment in enumerate(attachments):
            db.execute(update(chat_message_resources).where(
                chat_message_resources.c.message_id == user_message.id,
                chat_message_resources.c.attachment_id == attachment.id).values(position=position))
        message = ChatMessage(
            conversation_id=conversation.id, sequence=next_sequence - 1,
            role="assistant", status="streaming", request_id=payload.request_id,
        )
        db.add(message)
        if next_sequence == 2 and conversation.title == "新对话":
            from .conversation_jobs import temporary_title
            conversation.title = temporary_title(query or attachments[0].filename)
            conversation.revision += 1
        message.provider_id = UUID(provider_config.provider_id)
        message.provider_name = provider_config.provider_name
        message.model = provider_config.model
        message.thinking_snapshot = provider_config.thinking.snapshot()
        message.encrypted_continuation = None
        db.flush()
        from .conversation_jobs import enqueue, dispatch, public_status
        execution = enqueue(db, conversation.id, message.id, payload.request_id, access, hold_amount, provider_config, search_config)
        db.commit()
        result = public_status(execution)
        dispatch(payload.request_id)
        return result

    return router

async def generate(factory, conversation_id, message_id, request_id, access, hold_amount, provider_config, search_config=None, retrieve=None):
    manual = message_id is None
    final_status = "cancelled"
    error_detail = None
    content = ""
    citations = []
    web_sources = []
    traces = []
    tool_events = []
    display_blocks = []
    thinking_blocks = []
    thinking_diagnostics = []
    stopped = Event()
    sequence = 0
    retrieval_query = None
    refused = False
    refusal_reason = None
    continuation = None
    disconnected = False
    last_text_save = 0.0
    with factory() as metering_db:
        metered = metering_db.get(ModelUsageRequest, request_id) is not None
        from .workspace_billing import LegacyPriceMeter
    meter = (model_billing.ModelMeter(factory, request_id) if metered else
             LegacyPriceMeter(factory, access.workspace_id, request_id))

    def emit(name, data):
        nonlocal sequence
        sequence += 1
        payload = event(name, {**data, "request_id": str(request_id), "event_sequence": sequence})
        payload["id"] = f"{request_id}:{sequence}"
        return event_text(payload)

    def save(status="streaming"):
        with factory() as db:
            conversation = db.scalar(select(Conversation).where(
                Conversation.id == conversation_id,
            ).with_for_update())
            if manual:
                operation = db.get(ContextOperation, request_id)
                if status != "streaming":
                    if operation.status == "running":
                        operation.status = status
                        operation.result = {"status": "completed" if status == "completed" else "failed",
                            "error": error_detail, "eligible": False if status == "completed" else True}
                    if conversation.active_request_id == request_id:
                        conversation.active_request_id = None
                    db.commit()
                return {**operation.result, "request_id": str(request_id)}, status == "streaming"
            try:
                access.revalidate(db).conversation(db, conversation)
            except HTTPException:
                message = db.get(ChatMessage, message_id)
                if message.request_id == request_id and message.status == "streaming":
                    message.status = "interrupted"
                    message.error_detail = "访问权限已变更，回答已停止"
                if conversation.active_request_id == request_id:
                    conversation.active_request_id = None
                db.commit()
                result = message_read(message)
                result.update(content="", citations=[], web_sources=[], traces=[], tool_events=[], display_blocks=[])
                return result, False
            if conversation.active_request_id != request_id or conversation.deleted_at is not None:
                message = db.get(ChatMessage, message_id)
                if (conversation.deleted_at is None and message.request_id == request_id
                        and message.status == "cancelled"):
                    from .conversation_jobs import recover_steered_blocks
                    message.content = content
                    message.display_blocks = recover_steered_blocks(db, request_id, display_blocks)
                    message.tool_events = tool_events
                    message.web_sources = web_sources
                    message.encrypted_display_thinking = encrypt_display_thinking(thinking_blocks)
                    message.thinking_diagnostics = thinking_diagnostics
                    db.commit()
                return message_read(db.get(ChatMessage, message_id)), False
            row = db.execute(update(ChatMessage).where(
                ChatMessage.id == message_id, ChatMessage.request_id == request_id,
                ChatMessage.status == "streaming",
            ).values(
                content=content, status=status, citations=citations, web_sources=web_sources, traces=traces,
                tool_events=tool_events,
                display_blocks=display_blocks,
                retrieval_query=retrieval_query, refused=refused,
                refusal_reason=refusal_reason, error_detail=error_detail,
                encrypted_continuation=continuation,
                encrypted_display_thinking=encrypt_display_thinking(thinking_blocks),
                thinking_diagnostics=thinking_diagnostics,
            ))
            if row.rowcount and status != "streaming":
                db.execute(update(Conversation).where(
                    Conversation.id == conversation_id, Conversation.active_request_id == request_id,
                ).values(active_request_id=None, updated_at=datetime.now(timezone.utc)))
            db.commit()
            message = db.get(ChatMessage, message_id)
            return message_read(message), bool(row.rowcount)

    def still_authorized():
        if stopped.is_set():
            return False
        with factory() as db:
            checked = access.revalidate(db)
            conversation = live_conversation(db, conversation_id, checked)
            return conversation.active_request_id == request_id

    try:
        with factory() as db:
            checked_access = access.revalidate(db)
            conversation = live_conversation(db, conversation_id, checked_access)
            scope_ids = []
            message = db.get(ChatMessage, message_id) if not manual else None
            user_message = db.scalar(select(ChatMessage).where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.sequence == message.sequence - 1,
            )) if not manual else None
            query = user_content(user_message.content, user_message.attachments) if user_message else None
            retrieval_query = user_message.content if user_message else None
            current_knowledge_mentions = []
            current_document_mentions = []
            current_resource_mentions = []
            if user_message:
                current_resource_mentions = resource_references.resolve(db, checked_access,
                    [item["id"] for item in user_message.resource_mentions or []], skip_unavailable=True)
                for mention in user_message.knowledge_base_mentions or []:
                    try:
                        library = checked_access.library(db, UUID(mention["id"]))
                    except (HTTPException, KeyError, ValueError):
                        continue
                    if library.status != KnowledgeBaseStatus.ARCHIVED:
                        current_knowledge_mentions.append({"id": str(library.id), "name": library.name})
                for mention in user_message.document_mentions or []:
                    try:
                        document = db.get(Document, UUID(mention["id"]))
                        if document is None or document.status != DocumentStatus.READY or not document.is_active:
                            continue
                        library = checked_access.library(db, document.knowledge_base_id)
                    except (HTTPException, KeyError, ValueError):
                        continue
                    if library.status != KnowledgeBaseStatus.ARCHIVED:
                        current_document_mentions.append({
                            "id": str(document.id), "filename": document.filename,
                            "knowledge_base_id": str(library.id),
                            "knowledge_base_name": library.name,
                        })
        if not manual:
            yield emit("message", message_read(message))
        yield emit("phase", {"phase": "planning"})
        provider = OpenAICompatibleChatProvider(config=provider_config)
        provider.resource_context = (factory, access)
        if not manual and isinstance(getattr(provider, "config", None), ChatProviderConfig):
            from .llm_providers import refresh_chat_credentials

            def refresh_credentials():
                with factory() as db:
                    access.revalidate(db)
                    refreshed = refresh_chat_credentials(db, provider.config)
                if refreshed is None:
                    return False
                provider.config = refreshed
                return True

            provider.refresh_credentials = refresh_credentials
        if not manual and provider_config.responses_websocket:
            from .responses_socket import ResponsesSocket
            provider.responses_socket = ResponsesSocket()
        if not hasattr(provider, "complete_with_tools"):
            raise LLMProviderError("当前模型不支持工具调用，请配置支持工具调用的模型")
        agent_run = None
        extension_runtime = await run_in_threadpool(
            ExtensionRuntime, factory, access,
            conversation.skill_ids if conversation.extensions_initialized else [],
            conversation.mcp_server_ids if conversation.extensions_initialized else [],
            message_id, request_id,
            conversation.question_workbench,
            provider_config,
        )
        if not provider_config.thinking.supports_tools:
            extension_runtime.skills, extension_runtime.tools = [], []
        manager = ContextManager(factory, access, conversation_id, provider_config, provider, meter,
            request_id, manual=manual,
            skill_versions=extension_runtime.versions if provider_config.thinking.supports_tools else [])
        with factory() as db:
            conversation = live_conversation(db, conversation_id, access.revalidate(db))
            history = manager.history(db, conversation)
            db.commit()
            initial_messages, context_tools = build_messages(db, conversation, [], provider_config, extension_runtime, query, history=history)
            initial_messages = initial_messages[:1] + history + initial_messages[1:]
        manager.current_message_id = user_message.id if user_message else None
        manager.current_sequence = user_message.sequence if user_message else None
        if manual:
            initial_messages = manager.start(initial_messages[0])
            from queue import SimpleQueue
            def manual_execution():
                from concurrent.futures import ThreadPoolExecutor
                output = SimpleQueue()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(manager.prepare, initial_messages, context_tools + (
                        [HISTORY_TOOL] if provider_config.thinking.supports_tools else []),
                        output.put, lambda: None if still_authorized() else (_ for _ in ()).throw(AgentCancelled()))
                    while not future.done() or not output.empty():
                        try:
                            yield output.get(timeout=0.1)
                        except __import__("queue").Empty:
                            continue
                    compacted = future.result()
                yield "context", manager.state(compacted, context_tools + (
                    [HISTORY_TOOL] if provider_config.thinking.supports_tools else []))
            execution = manual_execution()
        else:
            from .search_billing import SearchRuntime
            from .conversation_jobs import steering_batch
            def read_steering(seal=False):
                return steering_batch(factory, conversation_id, request_id, seal=seal)
            execution = stream_retrieval_agent(
            query=query, knowledge_base_ids=scope_ids, history=history,
            factory=factory, access=access, retrieve=retrieve, provider=provider,
            is_active=still_authorized,
            knowledge_base_mentions=current_knowledge_mentions,
            extensions=extension_runtime,
            meter=meter,
            context_manager=manager,
            document_mentions=current_document_mentions,
            resource_mentions=current_resource_mentions,
            search=SearchRuntime(factory, access, request_id, search_config) if search_config else None,
            read_steering=read_steering,
        )
        bridge = stream_events(execution, stopped)
        async for name, data in bridge:
            if name == "heartbeat":
                yield ": heartbeat\n\n"
                continue
            now = time.monotonic()
            active = await run_in_threadpool(still_authorized)
            if not active and name not in ("tool_started", "tool_result", "compaction", "thinking_status", "text_complete", "steered"):
                continue
            if name == "complete":
                agent_run = data
            else:
                if name in ("thinking_delta", "thinking_status"):
                    block_id = data["block_id"]
                    block = next((item for item in display_blocks if item["id"] == block_id), None)
                    if name == "thinking_delta" and block is None:
                        block = {"id": block_id, "type": "thinking", "round": data["round"], "status": "running"}
                        display_blocks.append(block)
                        thinking_blocks.append({"id": block_id, "round": data["round"], "text": ""})
                    if name == "thinking_delta":
                        private_block = next(item for item in thinking_blocks if item["id"] == block_id)
                        private_block["text"] += data["text"]
                        if now - last_text_save >= 0.25:
                            await run_in_threadpool(save)
                            last_text_save = now
                    else:
                        if block:
                            block["status"] = data["status"]
                        if data.get("diagnostic") is not None:
                            diagnostic = {"round": data["round"], "status": data["status"], **data["diagnostic"]}
                            thinking_diagnostics = [
                                item for item in thinking_diagnostics if item["round"] != data["round"]
                            ] + [diagnostic]
                        await run_in_threadpool(save)
                if name == "delta":
                    block_id = data["block_id"]
                    block = next((item for item in display_blocks if item["id"] == block_id), None)
                    if block is None:
                        block = {"id": block_id, "type": "text", "text": "", "stage": "pending", "status": "running"}
                        display_blocks.append(block)
                        if content:
                            content += "\n\n"
                    block["text"] += data["text"]
                    content += data["text"]
                    if now - last_text_save >= 0.25:
                        await run_in_threadpool(save)
                        last_text_save = now
                if name == "text_complete":
                    block = next((item for item in display_blocks if item["id"] == data["block_id"]), None)
                    if block is not None:
                        block.update(stage=data["stage"], status=data["status"])
                        await run_in_threadpool(save)
                if name == "steered":
                    if not any(item["id"] == data["id"] for item in display_blocks):
                        display_blocks.append(data)
                        await run_in_threadpool(save)
                if name == "sources":
                    citations, traces = data["citations"], data["traces"]
                    await run_in_threadpool(save)
                if name == "web_sources":
                    web_sources = data["sources"]
                    await run_in_threadpool(save)
                if name in ("tool_call", "tool_started", "tool_result", "compaction"):
                    if name == "compaction":
                        data = {**data, "kind": "compaction", "name": "compact_context"}
                    summary = {
                        key: data[key] for key in ("call_id", "execution_id", "name", "label", "kind", "status", "outcome", "result_count", "summary", "files", "deliverables", "saved_document", "image_task", "preview", "cloud_computer", "computer_id", "command_id", "knowledge_base",
                                                   "skill_id", "skill_name", "skill_version")
                        if key in data
                    }
                    summary.setdefault("status", "running")
                    index = next((i for i, item in enumerate(tool_events)
                                  if item.get("call_id") == summary.get("call_id")), None)
                    if index is None:
                        tool_events.append(summary)
                        display_blocks.append({"id": f"activity-{summary['call_id']}",
                            "type": "activity", "call_id": summary["call_id"]})
                    else:
                        current = tool_events[index]
                        if current["status"] not in ("waiting", "running") and summary["status"] in ("waiting", "running"):
                            continue
                        tool_events[index] = {**current, **summary}
                    await run_in_threadpool(save)
                if active:
                    yield emit(name, data)
        if agent_run is None:
            if not manual:
                raise LLMProviderError("Agent 未完成回答，请重试")
            final_status = "completed"
            return
        citations, traces = agent_run.citations, agent_run.traces
        web_sources = agent_run.web_sources
        # Keep an encrypted audit copy for legacy display/export consumers. The
        # effective context manager never reads it as a recovery source.
        continuation = encrypt_continuation(provider_config, agent_run.protocol_messages)
        _, active = await run_in_threadpool(save)
        if not active:
            return
        yield emit("sources", {"citations": citations, "traces": traces})
        final_status = "completed"
    except AgentCancelled:
        final_status = "cancelled"
    except HTTPException as exc:
        from .tool_errors import public_error
        final_status = "interrupted"
        error_detail = ("访问权限已变更，回答已停止" if exc.status_code in (401, 403) else
            public_error(exc.detail))
    except (LLMProviderError, LLMConfigurationError) as exc:
        final_status = "failed"
        error_detail = str(exc)
        if getattr(exc, "code", None):
            yield emit("context_error", {"error_code": exc.code, "message": str(exc)})
    except (asyncio.CancelledError, GeneratorExit):
        disconnected = True
        raise
    except Exception:
        logging.getLogger(__name__).exception("Conversation execution failed for request %s", request_id)
        final_status = "failed"
        error_detail = "本次回答未能完成，请重试"
    finally:
        # Disconnect/cancel must still release the per-conversation generation slot.
        with anyio.CancelScope(shield=True):
            stopped.set()
            if "bridge" in locals():
                await bridge.aclose()
            if "provider" in locals() and getattr(provider, "responses_socket", None):
                await run_in_threadpool(provider.responses_socket.close)
            for item in tool_events:
                if item.get("status") in ("waiting", "running"):
                    item["status"] = "cancelled" if final_status == "cancelled" else "interrupted"
            for block in display_blocks:
                if block["type"] == "thinking" and block.get("status") == "running":
                    block["status"] = "cancelled" if final_status == "cancelled" else "interrupted"
                if block["type"] == "text" and block.get("status") == "running":
                    block.update(stage="intermediate", status="interrupted")
            if final_status in ("failed", "cancelled", "interrupted"):
                partial = next((block for block in reversed(display_blocks) if block["type"] == "text"), None)
                if partial and partial.get("stage") == "commentary":
                    partial["stage"] = "intermediate"
            if metered:
                with factory() as usage_db:
                    try:
                        model_billing.finalize_request(usage_db, request_id, final_status)
                        usage_db.commit()
                    except Exception:
                        usage_db.rollback()
                        logging.getLogger(__name__).exception("Model settlement deferred for request %s", request_id)
                        if final_status == "completed":
                            final_status = "failed"
                            error_detail = "回答已保留，用量尚未结算。可以查看积分记录或联系管理员"
            saved, _ = await run_in_threadpool(save, final_status)
            if not metered:
                with factory() as credit_db:
                    try:
                        from .workspace_billing import finish_operation, legacy_call_usage
                        settle_model_usage(credit_db, access.workspace_id, access.user_id, request_id,
                            legacy_call_usage(credit_db, access.workspace_id, request_id), hold_amount)
                        finish_operation(credit_db, access.workspace_id, request_id)
                        credit_db.commit()
                    except Exception:
                        credit_db.rollback()
                        logging.getLogger(__name__).error("Model settlement deferred for request %s", request_id)
        if not disconnected and saved["request_id"] == str(request_id):
            yield emit("done", saved)
