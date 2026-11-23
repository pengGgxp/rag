from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .. import model_billing
from .. import resource_mentions as resource_references
from ..agent.effective_context import ContextManager
from ..audit_log import record_audit
from ..auth import Access, get_access
from ..conversation_context import HISTORY_TOOL, build_messages
from ..credits import CreditError, authorization_hold, authorization_hold_amount
from ..db import get_db
from ..extensions import ExtensionRuntime, defaults as extension_defaults, validate_selection
from ..llm_providers import request_provider_config
from ..models import ChatMessage, ContextOperation, Conversation, Document, DocumentStatus, KnowledgeBase, KnowledgeBaseStatus, SiteModelProvider, ThinkingTest
from ..thinking import decrypt_display_thinking
from .attachments import select_attachments
from .messages import conversation_read, live_conversation, message_read, resolve_input_mentions
from .schemas import ContextRequest, ConversationCreate, ConversationRename, ExtensionSelection, SteerRequest, StopRequest, TurnRequest
from .stream import event, event_text, subscribe


router = APIRouter(prefix="/conversations", tags=["conversations"])

@router.get("/{conversation_id}/messages/{message_id}/citations/{chunk_id}")
def read_citation(conversation_id: UUID, message_id: UUID, chunk_id: UUID,
                  db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from ..knowledge.originals import resolve_citation

    live_conversation(db, conversation_id, access)
    message = db.get(ChatMessage, message_id)
    if message is None or message.conversation_id != conversation_id or message.role != "assistant":
        raise HTTPException(404, "回答不存在")
    citation = next((item for item in (message.citations or [])
                     if item.get("chunk_id") == str(chunk_id)), None)
    if citation is None:
        raise HTTPException(404, "引用不存在")
    return resolve_citation(db, access, citation, chunk_id)

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
        from ..question_workbench import status as workbench_status
        workbench = workbench_status(db, access)
        if not (workbench["available"] and workbench["enabled"]):
            raise HTTPException(403, "请先授权题目工作台")
    conversation = Conversation(
        title=payload.title, title_source="temporary" if payload.title == "新对话" else "manual",
        owner_id=access.user_id, account_id=access.user_id, extensions_initialized=True,
        skill_ids=list(dict.fromkeys(map(str, payload.skill_ids))),
        question_workbench=payload.question_workbench,
        mcp_server_ids=list(dict.fromkeys(map(str, payload.mcp_server_ids))),
    )
    db.add(conversation)
    db.flush()
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="conversation.created", resource_type="conversation",
        resource_id=conversation.id, metadata={"knowledge_base_count": 0}, request=request,
    )
    db.commit()
    db.refresh(conversation)
    return conversation_read(conversation, True)

@router.get("")
def listing(cursor: str | None = None, db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from .management import page
    return page(db, access, cursor, conversation_read)

@router.get("/search")
def search(q: str = "", cursor: str | None = None, since: datetime | None = None,
           until: datetime | None = None, knowledge_base_id: UUID | None = None,
           has_attachments: bool = False, db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from .management import search_page
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
    from ..models import ConversationExecution
    from .jobs import public_status
    live_conversation(db, conversation_id, access)
    row = db.get(ConversationExecution, request_id)
    if row is None or row.conversation_id != conversation_id:
        raise HTTPException(404, "回答不存在")
    return public_status(row)

@router.get("/{conversation_id}/requests/{request_id}/events")
def execution_events(conversation_id: UUID, request_id: UUID, after: int = 0,
                     db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from ..models import ConversationExecution
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
        from ..question_workbench import status as workbench_status
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
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
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
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
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
    from ..tool_detail import receipt_projection
    return JSONResponse(receipt_projection(db, access, conversation_id, request_id, execution_id, offset),
                        headers={"Cache-Control": "private, no-store"})

@router.post("/{conversation_id}/requests/{request_id}/steer")
def steer(conversation_id: UUID, request_id: UUID, payload: SteerRequest,
          db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from .jobs import update_steering
    conversation = live_conversation(db, conversation_id, access.revalidate(db))
    return update_steering(db, conversation, request_id, payload.message_id,
        payload.model_dump(mode="json", exclude={"message_id"}, exclude_defaults=True))

@router.delete("/{conversation_id}/requests/{request_id}/steer/{message_id}")
def withdraw_steering(conversation_id: UUID, request_id: UUID, message_id: UUID,
                      db: Session = Depends(get_db), access: Access = Depends(get_access)):
    from .jobs import update_steering
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
        from .jobs import recover_steered_blocks
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
    from ..agent.tool_jobs import cancel_request
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
                    f"字符 {citation['char_start']}–{citation['char_end']}，"
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
    from ..shared_providers import unavailable_reason
    from ..llm import ChatProviderConfig
    from ..thinking import select_thinking
    shared = db.get(SiteModelProvider, provider_id)
    if shared is None or unavailable_reason(db, shared, None, True):
        raise HTTPException(409, "当前模型不可用，请重新选择")
    from ..model_connections import connection_for
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
        from ..search_settings import runtime_config
        from ..web_search import tools as search_tools
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
        from ..models import ConversationExecution
        from .jobs import public_status
        execution = db.get(ConversationExecution, payload.request_id)
        if execution:
            return public_status(execution)
        async def repeat():
            yield event_text(event("done", existing.result))
        return StreamingResponse(repeat(), media_type="text/event-stream")
    if conversation.active_request_id:
        raise HTTPException(409, "请先停止或等待当前操作完成")
    config = request_provider_config(db, access.user_id, payload.provider_id,
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
    from .attachments import validate_messages
    validate_messages(history, config)
    # 检查点已覆盖可整理的前缀时，直接返回且不计费。
    if not manager.eligible():
        async def nothing():
            yield event_text(event("done", {"status": "idle", "eligible": False, "no_op": True}))
        db.commit()
        return StreamingResponse(nothing(), media_type="text/event-stream")
    hold = authorization_hold_amount()
    if model_billing.enabled(db):
        model_billing.start_request(db, access.user_id, access.user_id, payload.request_id,
            UUID(config.provider_id), hold)
    else:
        from ..workspace_billing import operation_snapshot
        model_billing.settle_pending(db, access.user_id, hold)
        operation_snapshot(db, access.user_id, payload.request_id, "model",
            capabilities=model_billing.CAPABILITIES, provider_id=UUID(config.provider_id))
        authorization_hold(db, access.user_id, access.user_id, payload.request_id, hold)
    conversation.active_request_id = payload.request_id
    db.add(ContextOperation(request_id=payload.request_id, conversation_id=conversation_id))
    from .jobs import enqueue, dispatch, public_status
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
    from ..models import ConversationExecution
    from .jobs import public_status
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
        from ..models import ConversationExecution
        from .jobs import public_status
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
        provider_config = request_provider_config(db, access.user_id, payload.provider_id,
            payload.thinking_option_id, payload.thinking_config_version)
        if not provider_config.thinking.supports_tools and any(current_mentions.values()):
            raise HTTPException(422, "当前模型无法使用资料引用，请选择支持工具的模型")
        attachments = select_attachments(db, access, [row.id for row in previous_user.attachments]
            if previous_user else payload.attachment_ids, provider_config)
        # 预留积分前检查保留的多模态历史，重试时也执行检查。
        from .attachments import validate_messages
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        manager = ContextManager(factory, access, conversation.id, provider_config, None, None, None)
        validate_messages(manager.history(db, conversation), provider_config)
        if metered:
            if payload.web_search and provider_config.thinking.supports_tools:
                from ..search_settings import runtime_config
                search_config = runtime_config(db)
            model_billing.start_request(db, access.user_id, access.user_id,
                payload.request_id, UUID(provider_config.provider_id), hold_amount, search_config=search_config)
        else:
            from ..workspace_billing import operation_snapshot
            model_billing.settle_pending(db, access.user_id, hold_amount)
            operation_snapshot(db, access.user_id, payload.request_id, "model",
                capabilities=model_billing.CAPABILITIES, provider_id=UUID(provider_config.provider_id))
            authorization_hold(db, access.user_id, access.user_id, payload.request_id, hold_amount)
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
    from ..models import chat_message_resources
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
        from .jobs import temporary_title
        conversation.title = temporary_title(query or attachments[0].filename)
        conversation.revision += 1
    message.provider_id = UUID(provider_config.provider_id)
    message.provider_name = provider_config.provider_name
    message.model = provider_config.model
    message.thinking_snapshot = provider_config.thinking.snapshot()
    message.encrypted_continuation = None
    db.flush()
    from .jobs import enqueue, dispatch, public_status
    execution = enqueue(db, conversation.id, message.id, payload.request_id, access, hold_amount, provider_config, search_config)
    db.commit()
    result = public_status(execution)
    dispatch(payload.request_id)
    return result
