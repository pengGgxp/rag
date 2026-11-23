from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..auth import Access, get_access
from ..db import get_db
from ..models import AnswerFeedback, ChatMessage, Conversation
from ..audit_log import record_audit

from .schemas import FeedbackWrite


def feedback_read(row: AnswerFeedback | None):
    if row is None:
        return None
    return {
        "id": str(row.id), "rating": row.rating,
        "categories": row.categories, "comment": row.comment,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def feedback_message(db: Session, access: Access, message_id: UUID, *, lock=False):
    message = db.get(ChatMessage, message_id)
    if message is None or message.role != "assistant":
        raise HTTPException(404, "回答不存在")
    statement = select(Conversation).where(Conversation.id == message.conversation_id)
    if lock:
        statement = statement.with_for_update()
    conversation = db.scalar(statement)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(404, "会话不存在或已删除")
    access.conversation(db, conversation)
    return message


router = APIRouter(tags=["feedback"])


@router.post("/feedback")
def write_feedback(
    payload: FeedbackWrite,
    request: Request,
    db: Session = Depends(get_db),
    access: Access = Depends(get_access),
):
    message = feedback_message(db, access, payload.message_id, lock=True)
    if message.status != "completed":
        raise HTTPException(409, "回答尚未完成，暂不能反馈")
    # 生产和测试数据库均用原子 upsert 处理重复请求。
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    values = {
        "rating": payload.rating, "categories": payload.categories,
        "comment": payload.comment or None,
    }
    db.execute(insert(AnswerFeedback).values(
        id=uuid4(), message_id=message.id, user_id=access.user_id,
        account_id=access.user_id, **values,
    ).on_conflict_do_update(
        index_elements=["message_id", "user_id"],
        set_={**values, "updated_at": func.now()},
    ))
    db.commit()
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="feedback.created_or_updated", resource_type="message",
        resource_id=message.id, metadata={"rating": payload.rating, "category_count": len(payload.categories)},
        request=request,
    )
    db.commit()
    row = db.scalar(select(AnswerFeedback).where(
        AnswerFeedback.message_id == message.id, AnswerFeedback.user_id == access.user_id,
    ).execution_options(populate_existing=True))
    return feedback_read(row)


@router.delete("/feedback/{message_id}", status_code=204)
def delete_feedback(
    message_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    access: Access = Depends(get_access),
):
    feedback_message(db, access, message_id, lock=True)
    db.execute(delete(AnswerFeedback).where(
        AnswerFeedback.message_id == message_id, AnswerFeedback.user_id == access.user_id,
    ))
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="feedback.deleted", resource_type="message", resource_id=message_id, request=request,
    )
    db.commit()
    return Response(status_code=204)
