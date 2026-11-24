"""将用户明确要求保存的会话产物送入标准文档入库流程。"""
import hashlib
import io
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..identity.auth import get_access
from app.infra.db import get_db
from ..knowledge.ingestion.admission import stage_upload, dispatch_ingestion
from ..models import (ChatMessage, Chunk, Conversation, ConversationDocumentSave, Document,
                     DocumentStatus, IngestionJob, JobState, KnowledgeBase, KnowledgeBaseStatus)

router = APIRouter(tags=["conversation-documents"])
LABELS = {"list_writable_knowledge_bases": "查看可保存的知识库", "save_to_knowledge_base": "保存资料",
          "get_saved_document_status": "查看资料处理进度"}


class SaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_id: UUID
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=200000)


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    save_id: UUID


def definition(name, description, schema):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


TOOLS = [
    definition("list_writable_knowledge_bases", "列出本人可保存资料的知识库，仅返回名称和ID，不扩大检索权限。",
        {"type": "object", "properties": {}, "additionalProperties": False}),
    definition("save_to_knowledge_base", "仅在用户明确要求或确认后，将确定的文字成果保存为新资料。按原上传规则处理并计费。不能保存思考、聊天日志或未经用户确认的资料。提交成功不代表已可检索。",
        SaveInput.model_json_schema()),
    definition("get_saved_document_status", "查询本人的成果保存任务状态，不重复上传；通常由界面展示进度，不必反复轮询。",
        StatusInput.model_json_schema()),
]


def saved_view(db, access, row):
    if row is None or row.user_id != access.user_id or row.account_id != access.user_id:
        raise HTTPException(404, "保存记录不存在")
    access.library(db, row.knowledge_base_id)
    document = db.get(Document, row.document_id, populate_existing=True)
    from ..knowledge.ingestion.jobs import latest_document_job
    latest = latest_document_job(db, document)
    state = document.status.value if document.is_active else "deleted"
    terminal = state in ("ready", "failed", "deleted") or bool(latest and latest.state == JobState.CANCELLED)
    summary = ("已存入，可供检索" if state == "ready" else "资料已删除" if state == "deleted"
        else "处理已取消，请在文档列表中重试" if latest and latest.state == JobState.CANCELLED
        else "处理未完成，请在文档列表中查看并重试" if state == "failed"
        else "已提交，正在处理")
    from ..knowledge.originals import mount_path
    return {"original_path": mount_path(document, access.user_id), "searchable": state == "ready",
        "save_id": str(row.id), "document_id": str(document.id), "knowledge_base_id": str(row.knowledge_base_id),
        "filename": document.filename, "document_status": state, "terminal": terminal, "summary": summary,
        "url": f"/?library={row.knowledge_base_id}&view=documents&document={document.id}",
        "content_origin": document.content_origin}


@router.get("/conversation-document-saves/{save_id}")
def read_saved(save_id: UUID, db=Depends(get_db), access=Depends(get_access)):
    return saved_view(db, access, db.get(ConversationDocumentSave, save_id))


def dispatch_save(db, row):
    row = db.get(ConversationDocumentSave, row.id, with_for_update=True, populate_existing=True)
    if not row.dispatch_pending:
        return
    job = db.get(IngestionJob, row.job_id, populate_existing=True)
    document = db.get(Document, row.document_id, populate_existing=True)
    from ..knowledge.ingestion.jobs import job_matches_original
    if job.state == JobState.QUEUED and document.is_active and document.status != DocumentStatus.DELETED and job_matches_original(document, job):
        try:
            dispatch_ingestion(job.id)
        except Exception:
            # The committed receipt is retried by API recovery, never by status reads.
            return
    row.dispatch_pending = False
    db.commit()


def recover_pending(factory):
    with factory() as db:
        identifiers = db.scalars(select(ConversationDocumentSave.id).where(ConversationDocumentSave.dispatch_pending.is_(True))
            .order_by(ConversationDocumentSave.created_at).limit(50)).all()
    for identifier in identifiers:
        with factory() as db:
            row = db.get(ConversationDocumentSave, identifier)
            if row:
                dispatch_save(db, row)


def verified_sources(db, access, message, markdown, current_sources):
    candidates = list(current_sources)
    previous = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == message.conversation_id,
        ChatMessage.sequence <= message.sequence, ChatMessage.role == "assistant")).all()
    for item in previous:
        candidates.extend(item.citations or [])
    result, seen = [], set()
    for item in candidates:
        raw = str(item.get("chunk_id", ""))
        if raw in seen or f"[{raw}]" not in markdown:
            continue
        try:
            chunk = db.get(Chunk, UUID(raw))
            document = db.get(Document, chunk.document_id) if chunk else None
            if not document or document.status == DocumentStatus.DELETED:
                continue
            access.library(db, document.knowledge_base_id)
        except (ValueError, HTTPException):
            continue
        result.append({"chunk_id": raw, "document_id": str(document.id), "original_hash": document.content_hash,
                       "page_number": item.get("page_number")})
        seen.add(raw)
    return result


def execute(runtime, name, arguments):
    with runtime.factory() as db:
        access = runtime.access.revalidate(db)
        if name == "get_saved_document_status":
            payload = StatusInput.model_validate(arguments)
            return saved_view(db, access, db.get(ConversationDocumentSave, payload.save_id))
        if name == "list_writable_knowledge_bases":
            if arguments:
                raise HTTPException(422, "此工具不接受参数")
            libraries = db.scalars(select(KnowledgeBase).where(access.visible_libraries(),
                KnowledgeBase.status != KnowledgeBaseStatus.ARCHIVED).order_by(KnowledgeBase.name, KnowledgeBase.id)).all()
            return {"knowledge_bases": [{"id": str(row.id), "name": row.name} for row in libraries]}
        payload = SaveInput.model_validate(arguments)
        title = payload.title.strip()
        if not title or any(ord(c) < 32 for c in title) or re.search(r"[/\\]", title) or not payload.markdown.strip() or "\x00" in payload.markdown:
            raise HTTPException(422, "请提供有效标题和非空文字内容")
        if not runtime.message_id or not runtime.request_id:
            raise HTTPException(409, "请在当前对话中保存资料")
        message = db.get(ChatMessage, runtime.message_id)
        conversation = db.get(Conversation, message.conversation_id) if message else None
        if not conversation or conversation.deleted_at is not None or conversation.owner_id != access.user_id or conversation.account_id != access.user_id or message.role != "assistant" or message.request_id != UUID(str(runtime.request_id)):
            raise HTTPException(404, "对话不存在或无权访问")
        access.library(db, payload.knowledge_base_id, write=True)
        from app.files.operations import workspace_lock
        content = payload.markdown.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        with workspace_lock(db, access.user_id):
            row = db.scalar(select(ConversationDocumentSave).where(
                ConversationDocumentSave.user_id == access.user_id,
                ConversationDocumentSave.request_id == UUID(str(runtime.request_id)),
                ConversationDocumentSave.knowledge_base_id == payload.knowledge_base_id,
                ConversationDocumentSave.content_hash == digest))
            if row is None:
                sources = verified_sources(db, access, message, payload.markdown, runtime.verified_citations)

                def finish(document, job, queued):
                    nonlocal row
                    if queued:
                        document.content_origin = "ai_assisted"
                    row = ConversationDocumentSave(account_id=access.user_id, user_id=access.user_id,
                        conversation_id=conversation.id, message_id=message.id, request_id=UUID(str(runtime.request_id)),
                        knowledge_base_id=payload.knowledge_base_id, document_id=document.id, job_id=job.id,
                        content_hash=digest, dispatch_pending=queued, source_citations=sources)
                    db.add(row)

                document = db.scalar(select(Document).where(Document.knowledge_base_id == payload.knowledge_base_id,
                    Document.content_hash == digest, Document.is_active.is_(True), Document.status != DocumentStatus.DELETED))
                if document is None:
                    stage_upload(db, access, payload.knowledge_base_id,
                        title if title.lower().endswith(".md") else title + ".md", "text/markdown", io.BytesIO(content),
                        finish=finish)
                else:
                    from ..knowledge.ingestion.jobs import latest_document_job
                    job = latest_document_job(db, document)
                    if job is None:
                        raise HTTPException(409, "已有同内容文档，请在知识库中查看")
                    finish(document, job, False)
                    db.commit()
            dispatch_save(db, row)
            return saved_view(db, access, row)
