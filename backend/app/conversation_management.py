"""Permission-filtered conversation pages and literal keyword search."""
import base64
import json
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Text, and_, case, cast, exists, or_, select

from .models import ChatMessage, Conversation, ConversationLibrary, chat_message_resources


def encode_cursor(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def decode_cursor(value, size):
    if not value:
        return None
    try:
        if len(value) > 2000:
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(value))
        if not isinstance(result, list) or len(result) != size:
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, "分页位置无效，请重新搜索") from None


def visible(access):
    return (
        Conversation.deleted_at.is_(None), Conversation.owner_id == access.user_id,
    )


def page(db, access, cursor, serialize):
    pin = case((Conversation.pinned_at.is_not(None), 1), else_=0)
    statement = select(Conversation).where(*visible(access))
    values = decode_cursor(cursor, 3)
    if values:
        try:
            rank, activity, identifier = int(values[0]), datetime.fromisoformat(values[1]), UUID(values[2])
        except (ValueError, TypeError):
            raise HTTPException(422, "分页位置无效，请重新加载") from None
        statement = statement.where(or_(pin < rank, and_(pin == rank, or_(
            Conversation.updated_at < activity,
            and_(Conversation.updated_at == activity, Conversation.id < identifier)))))
    rows = list(db.scalars(statement.order_by(pin.desc(), Conversation.updated_at.desc(), Conversation.id.desc()).limit(31)))
    items = rows[:30]
    last = items[-1] if items else None
    return {"items": [serialize(row) for row in items], "next_cursor": encode_cursor([
        int(last.pinned_at is not None), last.updated_at.isoformat(), str(last.id)]) if len(rows) > 30 else None}


def literal_pattern(query):
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def snippet(content, query):
    text = " ".join((content or "").split())
    position = text.casefold().find(query.casefold())
    start = max(0, position - 45)
    return ("…" if start else "") + text[start:start + 180] + ("…" if len(text) > start + 180 else "")


def search_page(db, access, query, cursor, since, until, knowledge_base_id, has_attachments):
    query = query.strip()
    if not query or len(query) > 200:
        raise HTTPException(422, "请输入 1–200 个字符")
    pattern = literal_pattern(query)
    title_hit = Conversation.title.ilike(pattern, escape="\\")
    message_hit = and_(ChatMessage.role.in_(["user", "assistant"]), ChatMessage.content.ilike(pattern, escape="\\"))
    rank = case((title_hit, 1), else_=0)
    statement = select(Conversation, rank.label("rank")).where(*visible(access), or_(title_hit,
        exists(select(ChatMessage.id).where(ChatMessage.conversation_id == Conversation.id, message_hit))))
    if since:
        statement = statement.where(Conversation.updated_at >= since)
    if until:
        statement = statement.where(Conversation.updated_at <= until)
    if knowledge_base_id:
        access.library(db, knowledge_base_id)
        library_pattern = f"%{knowledge_base_id}%"
        mentioned_or_retrieved = exists(select(ChatMessage.id).where(
            ChatMessage.conversation_id == Conversation.id,
            or_(
                cast(ChatMessage.knowledge_base_mentions, Text).ilike(library_pattern),
                cast(ChatMessage.document_mentions, Text).ilike(library_pattern),
                cast(ChatMessage.citations, Text).ilike(library_pattern),
                cast(ChatMessage.traces, Text).ilike(library_pattern),
            ),
        ))
        statement = statement.where(or_(
            Conversation.libraries.any(ConversationLibrary.knowledge_base_id == knowledge_base_id),
            mentioned_or_retrieved,
        ))
    if has_attachments:
        statement = statement.where(exists(select(ChatMessage.id).join(chat_message_resources,
            chat_message_resources.c.message_id == ChatMessage.id).where(ChatMessage.conversation_id == Conversation.id)))
    values = decode_cursor(cursor, 3)
    if values:
        try:
            previous_rank, activity, identifier = int(values[0]), datetime.fromisoformat(values[1]), UUID(values[2])
        except (ValueError, TypeError):
            raise HTTPException(422, "分页位置无效，请重新搜索") from None
        statement = statement.where(or_(rank < previous_rank, and_(rank == previous_rank, or_(
            Conversation.updated_at < activity, and_(Conversation.updated_at == activity, Conversation.id < identifier)))))
    rows = db.execute(statement.order_by(rank.desc(), Conversation.updated_at.desc(), Conversation.id.desc()).limit(21)).all()
    items = []
    for conversation, _ in rows[:20]:
        message = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id,
            message_hit).order_by(ChatMessage.sequence).limit(1))
        items.append({"id": str(conversation.id), "title": conversation.title,
            "updated_at": conversation.updated_at.isoformat(), "message_id": str(message.id) if message else None,
            "snippet": snippet(message.content if message else conversation.title, query)})
    last = rows[19] if len(rows) > 20 else None
    return {"items": items, "next_cursor": encode_cursor([last[1], last[0].updated_at.isoformat(), str(last[0].id)]) if last else None}
