import json
import time
from datetime import datetime, timezone
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .llm import LLMConfigurationError, LLMProviderError, OpenAICompatibleChatProvider
from .agent import AgentCancelled, stream_retrieval_agent
from .auth import Access, get_access
from .models import (
    ChatMessage,
    Conversation,
    ConversationLibrary,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from .feedback import feedback_read
from .audit_log import record_audit
from .credits import (
    CreditError, authorization_hold, authorization_hold_amount,
    release_hold, settle_model_usage,
)
from .extensions import ExtensionRuntime, validate_selection
from .llm_providers import active_provider_config
from . import model_billing
from .models import ModelUsageRequest


class ExtensionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_ids: list[UUID] = Field(default_factory=list)
    mcp_server_ids: list[UUID] = Field(default_factory=list)


class ConversationCreate(ExtensionSelection):
    model_config = ConfigDict(str_strip_whitespace=True)
    knowledge_base_ids: list[UUID] = Field(default_factory=list)
    title: str = Field(default="新对话", min_length=1, max_length=200)


class ConversationRename(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)


class TurnRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    request_id: UUID
    query: str | None = Field(default=None, min_length=1)
    retry_message_id: UUID | None = None

    @model_validator(mode="after")
    def one_operation(self):
        if (self.query is None) == (self.retry_message_id is None):
            raise ValueError("Provide either query or retry_message_id")
        return self


class StopRequest(BaseModel):
    request_id: UUID


def message_read(message: ChatMessage) -> dict:
    return {
        "id": str(message.id), "sequence": message.sequence, "role": message.role,
        "content": message.content, "status": message.status,
        "request_id": str(message.request_id) if message.request_id else None,
        "citations": message.citations, "traces": message.traces,
        "tool_events": message.tool_events,
        "refused": message.refused, "refusal_reason": message.refusal_reason,
        "retrieval_query": message.retrieval_query, "error_detail": message.error_detail,
        "feedback": feedback_read(message.feedback),
        "created_at": message.created_at.isoformat(),
    }


def conversation_read(conversation: Conversation, detail=False) -> dict:
    data = {
        "id": str(conversation.id), "title": conversation.title,
        "knowledge_bases": [
            {"id": str(item.knowledge_base_id), "name": item.library.name}
            for item in conversation.libraries
        ],
        "active_request_id": str(conversation.active_request_id) if conversation.active_request_id else None,
        "skill_ids": conversation.skill_ids, "mcp_server_ids": conversation.mcp_server_ids,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }
    if detail:
        data["messages"] = [message_read(message) for message in conversation.messages]
    return data


def live_conversation(db: Session, conversation_id: UUID, access: Access) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(404, "会话不存在或已删除")
    access.conversation(db, conversation)
    return conversation


def recover_interrupted_conversations(db: Session):
    model_billing.recover_requests(db)
    db.execute(update(ChatMessage).where(ChatMessage.status == "streaming").values(
        status="interrupted", error_detail="服务重启，回答已中断，可以重试",
    ))
    db.execute(update(Conversation).where(Conversation.active_request_id.is_not(None)).values(
        active_request_id=None,
    ))
    db.commit()


def event(name, data):
    return {"event": name, "data": json.dumps(data, ensure_ascii=False)}


def event_text(payload):
    return f"event: {payload['event']}\ndata: {payload['data']}\n\n"


def chat_router(get_db, retrieve):
    router = APIRouter(prefix="/conversations", tags=["conversations"])

    @router.post("", status_code=201)
    def create(payload: ConversationCreate, request: Request,
               db: Session = Depends(get_db), access: Access = Depends(get_access)):
        ids = list(dict.fromkeys(payload.knowledge_base_ids))
        validate_selection(db, access, payload.skill_ids, payload.mcp_server_ids)
        for id in ids:
            library = access.library(db, id)
            if library.status == KnowledgeBaseStatus.ARCHIVED:
                raise HTTPException(409, "归档知识库不能用于新对话")
        conversation = Conversation(
            title=payload.title, owner_id=access.user_id, workspace_id=access.workspace_id,
            skill_ids=list(dict.fromkeys(map(str, payload.skill_ids))),
            mcp_server_ids=list(dict.fromkeys(map(str, payload.mcp_server_ids))), libraries=[
            ConversationLibrary(knowledge_base_id=id) for id in ids
        ])
        db.add(conversation)
        db.flush()
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="conversation.created", resource_type="conversation",
            resource_id=conversation.id, metadata={"knowledge_base_count": len(ids)}, request=request,
        )
        db.commit()
        db.refresh(conversation)
        return conversation_read(conversation, True)

    @router.get("")
    def listing(db: Session = Depends(get_db), access: Access = Depends(get_access)):
        rows = db.scalars(select(Conversation).where(
            Conversation.deleted_at.is_(None),
            Conversation.owner_id == access.user_id, Conversation.workspace_id == access.workspace_id,
        ).order_by(Conversation.updated_at.desc(), Conversation.id)).all()
        visible = set(db.scalars(select(KnowledgeBase.id).where(access.visible_libraries())).all())
        return [conversation_read(row) for row in rows
                if all(item.knowledge_base_id in visible for item in row.libraries)]

    @router.get("/{conversation_id}")
    def detail(conversation_id: UUID, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        return conversation_read(live_conversation(db, conversation_id, access), True)

    @router.put("/{conversation_id}/extensions")
    def extensions(conversation_id: UUID, payload: ExtensionSelection,
                   db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.deleted_at.is_(None),
        ).with_for_update())
        if conversation is None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        if conversation.active_request_id:
            raise HTTPException(409, "请先停止或等待当前回答完成")
        validate_selection(db, access, payload.skill_ids, payload.mcp_server_ids)
        conversation.skill_ids = list(dict.fromkeys(map(str, payload.skill_ids)))
        conversation.mcp_server_ids = list(dict.fromkeys(map(str, payload.mcp_server_ids)))
        db.commit()
        return conversation_read(conversation, True)

    @router.patch("/{conversation_id}")
    def rename(conversation_id: UUID, payload: ConversationRename, request: Request,
               db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = live_conversation(db, conversation_id, access)
        conversation.title = payload.title
        record_audit(
            db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
            action="conversation.renamed", resource_type="conversation",
            resource_id=conversation.id, metadata={"title": payload.title[:100]}, request=request,
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
        if message is None:
            raise HTTPException(404, "回答不存在")
        db.execute(update(ChatMessage).where(
            ChatMessage.id == message.id, ChatMessage.status == "streaming",
        ).values(status="cancelled", error_detail=None))
        db.execute(update(Conversation).where(
            Conversation.id == conversation.id, Conversation.active_request_id == payload.request_id,
        ).values(active_request_id=None))
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "请求已被提交，请刷新对话后重试") from exc
        db.refresh(message)
        return message_read(message)

    @router.get("/{conversation_id}/export")
    def export(conversation_id: UUID, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = live_conversation(db, conversation_id, access)
        lines = [
            f"# {conversation.title}", "",
            "知识库：" + "、".join(item.library.name for item in conversation.libraries), "",
        ]
        for message in conversation.messages:
            lines.extend([f"## {'你' if message.role == 'user' else '知库'}", "", message.content or "（未生成正文）", ""])
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
        return Response(
            "\n".join(lines), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="conversation-{conversation.id}.md"'},
        )

    @router.post("/{conversation_id}/messages")
    def send(conversation_id: UUID, payload: TurnRequest, db: Session = Depends(get_db), access: Access = Depends(get_access)):
        conversation = db.scalar(select(Conversation).where(
            Conversation.id == conversation_id, Conversation.deleted_at.is_(None),
        ).with_for_update())
        if conversation is None:
            raise HTTPException(404, "会话不存在或已删除")
        access.conversation(db, conversation)
        existing = db.scalar(select(ChatMessage).where(ChatMessage.request_id == payload.request_id))
        if existing:
            if existing.conversation_id != conversation.id or existing.status == "streaming":
                raise HTTPException(409, "该请求正在处理或已被使用")
            async def duplicate():
                yield event_text(event("done", message_read(existing)))
            return StreamingResponse(duplicate(), media_type="text/event-stream")
        retry = None
        if payload.retry_message_id:
            retry = db.get(ChatMessage, payload.retry_message_id)
            latest = db.scalar(select(ChatMessage).where(
                ChatMessage.conversation_id == conversation.id,
            ).order_by(ChatMessage.sequence.desc()).limit(1))
            if (retry is None or retry.conversation_id != conversation.id or latest is None or latest.id != retry.id
                    or retry.role != "assistant" or retry.status not in ("failed", "cancelled", "interrupted")):
                raise HTTPException(409, "只能重试最后一条未完成的回答")
        try:
            hold_amount = authorization_hold_amount()
            metered = model_billing.enabled(db)
            provider_config = active_provider_config(db, access.workspace_id)
            if metered:
                model_billing.start_request(db, access.workspace_id, access.user_id,
                    payload.request_id, UUID(provider_config.provider_id), hold_amount)
            else:
                authorization_hold(db, access.workspace_id, access.user_id, payload.request_id, hold_amount)
        except CreditError:
            db.rollback()
            raise
        next_sequence = db.scalar(update(Conversation).where(
            Conversation.id == conversation.id, Conversation.deleted_at.is_(None),
            Conversation.active_request_id.is_(None),
        ).values(
            active_request_id=payload.request_id,
            next_sequence=Conversation.next_sequence + (0 if retry else 2),
            updated_at=datetime.now(timezone.utc),
        ).returning(Conversation.next_sequence))
        if next_sequence is None:
            db.rollback()
            raise HTTPException(409, "当前会话正在回答，请先停止或等待完成")
        if retry:
            message = retry
            message.request_id = payload.request_id
            message.content = ""
            message.status = "streaming"
            message.error_detail = None
            message.refused = False
            message.refusal_reason = None
            message.citations = []
            message.traces = []
            message.tool_events = []
        else:
            db.add(ChatMessage(
                conversation_id=conversation.id, sequence=next_sequence - 2,
                role="user", content=payload.query,
            ))
            message = ChatMessage(
                conversation_id=conversation.id, sequence=next_sequence - 1,
                role="assistant", status="streaming", request_id=payload.request_id,
            )
            db.add(message)
            if next_sequence == 2 and conversation.title == "新对话":
                conversation.title = payload.query[:40]
        db.commit()
        db.refresh(message)
        message_id = message.id
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        return StreamingResponse(
            generate(factory, conversation.id, message_id, payload.request_id, access, hold_amount, provider_config),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def generate(factory, conversation_id, message_id, request_id, access, hold_amount, provider_config):
        final_status = "cancelled"
        error_detail = None
        content = ""
        citations = []
        traces = []
        tool_events = []
        retrieval_query = None
        refused = False
        refusal_reason = None
        usage_settled = False
        with factory() as metering_db:
            metered = metering_db.get(ModelUsageRequest, request_id) is not None
        meter = model_billing.ModelMeter(factory, request_id) if metered else None

        def save(status="streaming"):
            with factory() as db:
                conversation = db.scalar(select(Conversation).where(
                    Conversation.id == conversation_id,
                ).with_for_update())
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
                    result.update(content="", citations=[], traces=[], tool_events=[])
                    return result, False
                if conversation.active_request_id != request_id or conversation.deleted_at is not None:
                    return message_read(db.get(ChatMessage, message_id)), False
                row = db.execute(update(ChatMessage).where(
                    ChatMessage.id == message_id, ChatMessage.request_id == request_id,
                    ChatMessage.status == "streaming",
                ).values(
                    content=content, status=status, citations=citations, traces=traces,
                    tool_events=tool_events,
                    retrieval_query=retrieval_query, refused=refused,
                    refusal_reason=refusal_reason, error_detail=error_detail,
                ))
                if row.rowcount and status != "streaming":
                    db.execute(update(Conversation).where(
                        Conversation.id == conversation_id, Conversation.active_request_id == request_id,
                    ).values(active_request_id=None, updated_at=datetime.now(timezone.utc)))
                db.commit()
                message = db.get(ChatMessage, message_id)
                return message_read(message), bool(row.rowcount)

        def still_authorized():
            with factory() as db:
                checked = access.revalidate(db)
                conversation = live_conversation(db, conversation_id, checked)
                return conversation.active_request_id == request_id

        try:
            with factory() as db:
                conversation = live_conversation(db, conversation_id, access.revalidate(db))
                scope_ids = [item.knowledge_base_id for item in conversation.libraries]
                skill_ids, server_ids = conversation.skill_ids, conversation.mcp_server_ids
                message = db.get(ChatMessage, message_id)
                query = db.scalar(select(ChatMessage.content).where(
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.sequence == message.sequence - 1,
                ))
                rows = db.scalars(select(ChatMessage).where(
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.sequence < message.sequence - 1,
                    ChatMessage.status == "completed",
                ).order_by(ChatMessage.sequence)).all()
                history = [{"role": row.role, "content": row.content} for row in rows]
            yield event_text(event("message", message_read(message)))
            yield event_text(event("phase", {"phase": "planning"}))
            provider = OpenAICompatibleChatProvider(config=provider_config)
            retrieval_query = query
            if not hasattr(provider, "complete_with_tools"):
                raise LLMProviderError("当前模型不支持工具调用，请配置支持工具调用的模型")
            agent_run = None
            extension_runtime = await run_in_threadpool(
                ExtensionRuntime, factory, access, skill_ids, server_ids,
            )
            execution = stream_retrieval_agent(
                query=query, knowledge_base_ids=scope_ids, history=history,
                factory=factory, access=access, retrieve=retrieve, provider=provider,
                is_active=still_authorized,
                extensions=extension_runtime,
                meter=meter,
            )
            async for name, data in iterate_in_threadpool(execution):
                if not await run_in_threadpool(still_authorized):
                    return
                if name == "complete":
                    agent_run = data
                else:
                    if name in ("tool_call", "tool_result"):
                        summary = {
                            key: data[key] for key in ("call_id", "name", "label", "kind", "status", "result_count")
                            if key in data
                        }
                        summary.setdefault("status", "running")
                        index = next((i for i, item in enumerate(tool_events)
                                      if item.get("call_id") == summary.get("call_id")), None)
                        if index is None:
                            tool_events.append(summary)
                        else:
                            tool_events[index] = {**tool_events[index], **summary}
                        await run_in_threadpool(save)
                    yield event_text(event(name, data))
            if agent_run is None:
                raise LLMProviderError("Agent 未完成回答，请重试")
            citations, traces = agent_run.citations, agent_run.traces
            if not meter:
                with factory() as usage_db:
                    settle_model_usage(
                        usage_db, access.workspace_id, access.user_id, request_id,
                        agent_run.usage_events, hold_amount,
                    )
                    usage_db.commit()
                usage_settled = True
            _, active = await run_in_threadpool(save)
            if not active:
                return
            yield event_text(event("sources", {"citations": citations, "traces": traces}))
            yield event_text(event("phase", {"phase": "answering"}))
            answer = agent_run.answer or ""
            last_save = 0.0
            for offset in range(0, len(answer), 120):
                delta = answer[offset:offset + 120]
                if not await run_in_threadpool(still_authorized):
                    return
                content += delta
                if time.monotonic() - last_save >= 0.4:
                    _, active = await run_in_threadpool(save)
                    if not active:
                        return
                    last_save = time.monotonic()
                yield event_text(event("delta", {"text": delta}))
            final_status = "completed"
        except AgentCancelled:
            final_status = "cancelled"
        except HTTPException:
            final_status = "interrupted"
            error_detail = "访问权限已变更，回答已停止"
        except (LLMProviderError, LLMConfigurationError) as exc:
            final_status = "failed"
            error_detail = str(exc)
        except Exception as exc:
            final_status = "failed"
            error_detail = f"本次回答未能完成，请重试: {exc}"
        finally:
            # Disconnect/cancel must still release the per-conversation generation slot.
            with anyio.CancelScope(shield=True):
                if meter:
                    with factory() as usage_db:
                        model_billing.finalize_request(usage_db, request_id, final_status)
                        usage_db.commit()
                    usage_settled = True
                saved, _ = await run_in_threadpool(save, final_status)
                if not meter and (final_status != "completed" or not usage_settled):
                    with factory() as credit_db:
                        try:
                            release_hold(
                                credit_db,
                                access.workspace_id,
                                access.user_id,
                                request_id,
                                hold_amount,
                            )
                            credit_db.commit()
                        except Exception:
                            credit_db.rollback()
            if final_status in ("completed", "failed", "interrupted") and saved["request_id"] == str(request_id):
                yield event_text(event("done", saved))

    return router
