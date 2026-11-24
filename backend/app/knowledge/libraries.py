from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.operations.audit_log import record_audit
from ..identity.auth import Access
from ..models import Document, DocumentStatus, KnowledgeBase, KnowledgeBaseStatus
from .schemas import KnowledgeBaseCreate, KnowledgeBaseRead, KnowledgeBaseUpdate


def create_knowledge_base(payload: KnowledgeBaseCreate, request: Request,
                          db: Session, access: Access):
    knowledge_base = KnowledgeBase(
        name=payload.name,
        description=payload.description,
        tags=payload.tags,
        account_id=access.user_id,
        owner_id=access.user_id,
    )
    db.add(knowledge_base)
    db.flush()
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="knowledge_base.created", resource_type="knowledge_base",
        resource_id=knowledge_base.id,
        metadata={"name": payload.name[:100], "tags": payload.tags},
        request=request,
    )
    db.commit()
    db.refresh(knowledge_base)
    return KnowledgeBaseRead.model_validate(knowledge_base)


def update_knowledge_base(
    knowledge_base_id: UUID,
    payload: KnowledgeBaseUpdate,
    request: Request,
    db: Session,
    access: Access,
):
    knowledge_base = access.library(
        db, knowledge_base_id, write=True, allow_archived=True,
    )
    knowledge_base.name = payload.name
    knowledge_base.description = payload.description
    if payload.tags is not None:
        knowledge_base.tags = payload.tags
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="knowledge_base.updated", resource_type="knowledge_base",
        resource_id=knowledge_base.id,
        metadata={
            "name": payload.name[:100],
            "has_description": bool(payload.description),
            "tags": payload.tags if payload.tags is not None else knowledge_base.tags,
        },
        request=request,
    )
    db.commit()
    db.refresh(knowledge_base)
    return KnowledgeBaseRead.model_validate(knowledge_base)


def archive_knowledge_base(
    knowledge_base_id: UUID,
    request: Request,
    db: Session,
    access: Access,
):
    knowledge_base = access.library(
        db, knowledge_base_id, write=True, allow_archived=True,
    )
    if knowledge_base.status == KnowledgeBaseStatus.ARCHIVED:
        return KnowledgeBaseRead.model_validate(knowledge_base)
    knowledge_base.status = KnowledgeBaseStatus.ARCHIVED
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="knowledge_base.archived", resource_type="knowledge_base",
        resource_id=knowledge_base.id, request=request,
    )
    db.commit()
    db.refresh(knowledge_base)
    return KnowledgeBaseRead.model_validate(knowledge_base)


def restore_knowledge_base(
    knowledge_base_id: UUID,
    request: Request,
    db: Session,
    access: Access,
):
    knowledge_base = access.library(db, knowledge_base_id, write=True, allow_archived=True)
    if knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
        return KnowledgeBaseRead.model_validate(knowledge_base)
    active_documents = db.scalars(select(Document).where(
        Document.knowledge_base_id == knowledge_base.id,
        Document.is_active.is_(True), Document.status != DocumentStatus.DELETED,
    )).all()
    knowledge_base.status = (
        KnowledgeBaseStatus.READY
        if any(document.status == DocumentStatus.READY for document in active_documents)
        else KnowledgeBaseStatus.DRAFT
    )
    record_audit(
        db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="knowledge_base.restored", resource_type="knowledge_base",
        resource_id=knowledge_base.id, request=request,
    )
    db.commit()
    db.refresh(knowledge_base)
    return KnowledgeBaseRead.model_validate(knowledge_base)


def list_knowledge_bases(db: Session, access: Access):
    rows = db.execute(
        select(KnowledgeBase, func.count(Document.id))
        .outerjoin(
            Document,
            (Document.knowledge_base_id == KnowledgeBase.id)
            & (Document.status != DocumentStatus.DELETED)
            & Document.is_active.is_(True),
        )
        .where(access.visible_libraries())
        .group_by(KnowledgeBase.id)
        .order_by(KnowledgeBase.created_at.desc())
    ).all()
    return [
        KnowledgeBaseRead.model_validate(kb).model_copy(update={"document_count": count})
        for kb, count in rows
    ]
