"""执行回答并保存消息、工具结果和用量。"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from threading import Event
from uuid import UUID

import anyio
from fastapi import HTTPException
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from .. import model_billing
from .. import resource_mentions as resource_references
from ..agent.effective_context import ContextManager
from ..agent.loop import AgentCancelled, stream_retrieval_agent
from ..conversation_context import HISTORY_TOOL, build_messages
from ..credits import settle_model_usage
from ..extensions import ExtensionRuntime
from ..llm import ChatProviderConfig, LLMConfigurationError, LLMProviderError, OpenAICompatibleChatProvider
from ..models import ChatMessage, ContextOperation, Conversation, Document, DocumentStatus, KnowledgeBaseStatus, ModelUsageRequest
from ..thinking import encrypt_continuation, encrypt_display_thinking
from .attachments import user_content
from .messages import live_conversation, message_read
from .stream import event, event_text, events as stream_events

logger = logging.getLogger("app.conversations")


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
        from ..workspace_billing import LegacyPriceMeter
        meter = (model_billing.ModelMeter(factory, request_id) if metered else
                 LegacyPriceMeter(factory, access.user_id, request_id))

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
                    from .jobs import recover_steered_blocks
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
            from ..llm_providers import refresh_chat_credentials

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
            from ..responses_socket import ResponsesSocket
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
            from ..search_billing import SearchRuntime
            from .jobs import steering_batch
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
        # 加密副本供旧版展示与导出读取，有效上下文不依赖此副本恢复。
        continuation = encrypt_continuation(provider_config, agent_run.protocol_messages)
        _, active = await run_in_threadpool(save)
        if not active:
            return
        yield emit("sources", {"citations": citations, "traces": traces})
        final_status = "completed"
    except AgentCancelled:
        final_status = "cancelled"
    except HTTPException as exc:
        from ..tool_errors import public_error
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
        logger.exception("Conversation execution failed for request %s", request_id)
        final_status = "failed"
        error_detail = "本次回答未能完成，请重试"
    finally:
        # 断线或取消时也要释放对话的生成占用。
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
                        logger.exception("Model settlement deferred for request %s", request_id)
                        if final_status == "completed":
                            final_status = "failed"
                            error_detail = "回答已保留，用量尚未结算。可以查看积分记录或联系管理员"
            saved, _ = await run_in_threadpool(save, final_status)
            if not metered:
                with factory() as credit_db:
                    try:
                        from ..workspace_billing import finish_operation, legacy_call_usage
                        partition = access.user_id
                        settle_model_usage(credit_db, partition, access.user_id, request_id,
                            legacy_call_usage(credit_db, partition, request_id), hold_amount)
                        finish_operation(credit_db, partition, request_id)
                        credit_db.commit()
                    except Exception:
                        credit_db.rollback()
                        logger.error("Model settlement deferred for request %s", request_id)
        if not disconnected and saved["request_id"] == str(request_id):
            yield emit("done", saved)
