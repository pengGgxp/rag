from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

LEGACY_WORKSPACE_ID = UUID("00000000-0000-4000-a000-000000000001")


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_site_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_granted: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_spent: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LLMProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_llm_provider_workspace_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    timeout_seconds: Mapped[float] = mapped_column(default=60)
    enabled: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class UsagePolicy(Base):
    __tablename__ = "usage_policies"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider_id", "capability",
            name="uq_usage_policy_workspace_provider_capability",
        ),
        CheckConstraint("credits_per_unit >= 0 AND unit_size > 0", name="ck_usage_policy_positive"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    provider_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("llm_provider_configs.id"), nullable=True, index=True,
    )
    capability: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(40))
    unit_size: Mapped[int] = mapped_column(Integer, default=1)
    credits_per_unit: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_usage_event_idempotency"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    provider_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("llm_provider_configs.id"), nullable=True, index=True,
    )
    provider_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    capability: Mapped[str] = mapped_column(String(80), index=True)
    units: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[int] = mapped_column(Integer)
    policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ModelBillingSettings(Base):
    __tablename__ = "model_billing_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    template: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ModelUsageRequest(Base):
    __tablename__ = "model_usage_requests"
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    hold: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[int] = mapped_column(Integer, default=0)
    formula_version: Mapped[str] = mapped_column(String(32), default="model_request_v2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelCallUsage(Base):
    __tablename__ = "model_call_usage"
    __table_args__ = (UniqueConstraint("request_id", "round", name="uq_model_call_round"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("model_usage_requests.request_id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[UUID | None] = mapped_column(ForeignKey("llm_provider_configs.id"), nullable=True)
    provider_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="started")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    prices: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RechargeOrder(Base):
    __tablename__ = "recharge_orders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "idempotency_key", name="uq_recharge_request"),
        UniqueConstraint("merchant_num", "platform_order_no", name="uq_recharge_platform_order"),
        CheckConstraint("amount_fen > 0 AND credits > 0", name="ck_recharge_positive"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    package_id: Mapped[str] = mapped_column(String(64))
    package_name: Mapped[str] = mapped_column(String(100))
    amount_fen: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    merchant_num: Mapped[str] = mapped_column(String(100))
    pay_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="creating")
    platform_order_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pay_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class Membership(Base):
    __tablename__ = "memberships"
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    provider: Mapped[str] = mapped_column(String(500), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)


class AuthenticationFlow(Base):
    __tablename__ = "authentication_flows"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SiteAuthSettings(Base):
    __tablename__ = "site_auth_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SiteAuditEvent(Base):
    __tablename__ = "site_audit_events"
    __table_args__ = (Index("ix_site_audit_events_action", "action"), Index("ix_site_audit_events_target", "target_type", "target_id"))
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[str] = mapped_column(String(20), default="success")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SiteAuthAudit(Base):
    __tablename__ = "site_auth_audit"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeBaseStatus(str, Enum):
    DRAFT = "draft"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"
    ARCHIVED = "archived"


class DocumentStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EmbeddingStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True, default=LEGACY_WORKSPACE_ID)
    restricted: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[KnowledgeBaseStatus] = mapped_column(
        SqlEnum(KnowledgeBaseStatus), default=KnowledgeBaseStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    version_group_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True, default=uuid4)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    parsed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parsing_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        SqlEnum(DocumentStatus), default=DocumentStatus.QUEUED
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    jobs: Mapped[list["IngestionJob"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_locations: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_status: Mapped[EmbeddingStatus] = mapped_column(
        SqlEnum(EmbeddingStatus), default=EmbeddingStatus.PENDING, nullable=False
    )
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[Document] = relationship(back_populates="chunks")

    @property
    def page_number(self) -> int | None:
        return self.source_locations[0]["page_number"] if self.source_locations else None


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    state: Mapped[JobState] = mapped_column(SqlEnum(JobState), default=JobState.QUEUED)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    document: Mapped[Document] = relationship(back_populates="jobs")


class QueryTrace(Base):
    __tablename__ = "query_traces"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_bases.id"), index=True
    )
    query: Mapped[str] = mapped_column(Text)
    retrieval_mode: Mapped[str] = mapped_column(String(50))
    embedding_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True, default=LEGACY_WORKSPACE_ID)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    skill_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    mcp_server_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)
    active_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)
    libraries: Mapped[list["ConversationLibrary"]] = relationship(cascade="all, delete-orphan")
    messages: Mapped[list["ChatMessage"]] = relationship(cascade="all, delete-orphan", order_by="ChatMessage.sequence")


class ConversationLibrary(Base):
    __tablename__ = "conversation_libraries"

    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"), primary_key=True)
    library: Mapped[KnowledgeBase] = relationship()


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "sequence", name="uq_chat_message_sequence"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, unique=True)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    traces: Mapped[list[dict]] = mapped_column(JSON, default=list)
    tool_events: Mapped[list[dict]] = mapped_column(JSON, default=list)
    retrieval_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    refused: Mapped[bool] = mapped_column(default=False)
    refusal_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    feedback: Mapped["AnswerFeedback | None"] = relationship(
        uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )


class AnswerFeedback(Base):
    __tablename__ = "answer_feedback"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(ForeignKey("chat_messages.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    rating: Mapped[str] = mapped_column(String(10))
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(50), index=True)
    resource_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LibraryGrant(Base):
    __tablename__ = "library_grants"
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class AgentSkill(Base):
    __tablename__ = "agent_skills"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    markdown: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
    files: Mapped[list["AgentSkillFile"]] = relationship(
        cascade="all, delete-orphan",
    )


class AgentSkillFile(Base):
    __tablename__ = "agent_skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("agent_skills.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[bytes] = mapped_column(LargeBinary)


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(String(30), default="streamable_http")
    encrypted_headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    header_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    tools: Mapped[list[dict]] = mapped_column(JSON, default=list)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

