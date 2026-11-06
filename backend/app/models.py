from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Table, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

LEGACY_WORKSPACE_ID = UUID("00000000-0000-4000-a000-000000000001")

chat_message_resources = Table(
    "chat_message_resources", Base.metadata,
    Column("message_id", PGUUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True),
    Column("attachment_id", PGUUID(as_uuid=True), ForeignKey("chat_attachments.id", ondelete="CASCADE"), primary_key=True),
    Column("position", Integer, nullable=False, server_default="0"),
)


class SiteSettings(Base):
    __tablename__ = "site_settings"
    __table_args__ = (
        CheckConstraint("signup_credits BETWEEN 0 AND 1000000", name="ck_site_signup_credits"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    signup_credits: Mapped[int] = mapped_column(Integer, default=1000)
    announcement_title: Mapped[str] = mapped_column(String(120), default="")
    announcement_body: Mapped[str] = mapped_column(Text, default="")
    administrator_contact: Mapped[str] = mapped_column(Text, default="", server_default="")
    announcement_published: Mapped[bool] = mapped_column(Boolean, default=False)
    announcement_revision: Mapped[int] = mapped_column(Integer, default=0)
    announcement_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SiteSearchSettings(Base):
    __tablename__ = "site_search_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=20)
    result_count: Mapped[int] = mapped_column(Integer, default=5)
    max_search_calls: Mapped[int] = mapped_column(Integer, default=3)
    max_fetch_calls: Mapped[int] = mapped_column(Integer, default=5)
    prices: Mapped[dict] = mapped_column(JSON, default=dict)


class SiteStorageSettings(Base):
    __tablename__ = "site_storage_settings"
    __table_args__ = (
        Index("uq_storage_provider", "provider", unique=True),
        Index("uq_storage_active", "active", unique=True,
            postgresql_where=text("active = true"), sqlite_where=text("active = 1")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(12), default="cos", server_default="cos")
    version: Mapped[int] = mapped_column(Integer, default=0)
    bucket: Mapped[str] = mapped_column(String(100), default="")
    region: Mapped[str] = mapped_column(String(60), default="")
    encrypted_secret_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_secret_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    tested_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_result: Mapped[str | None] = mapped_column(String(200), nullable=True)


class StorageMigration(Base):
    __tablename__ = "storage_migrations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_provider: Mapped[str] = mapped_column(String(12))
    target_provider: Mapped[str] = mapped_column(String(12))
    source_version: Mapped[int] = mapped_column(Integer)
    target_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="copying")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageMigrationObject(Base):
    __tablename__ = "storage_migration_objects"
    migration_id: Mapped[UUID] = mapped_column(ForeignKey("storage_migrations.id"), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(700), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(150))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)


class DocumentUpload(Base):
    __tablename__ = "document_uploads"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    version_of: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    multipart_id: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(24), default="uploading", index=True)
    parts: Mapped[dict] = mapped_column(JSON, default=dict)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_site_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    question_workbench_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    question_workbench_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (CheckConstraint("billing_multiplier_percent BETWEEN 1 AND 10000",
                                     name="ck_workspace_billing_multiplier"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    model_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    billing_multiplier_percent: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    billing_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    lifetime_granted: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    lifetime_spent: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ModelConnection(Base):
    __tablename__ = "model_connections"
    __table_args__ = (CheckConstraint("api_protocol IN ('chat_completions', 'responses', 'anthropic', 'gemini')",
                                     name="ck_model_connection_protocol"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    website_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    platform: Mapped[str] = mapped_column(String(30), default="custom")
    api_protocol: Mapped[str] = mapped_column(String(30), default="chat_completions",
                                             server_default="chat_completions")
    base_url: Mapped[str] = mapped_column(Text)
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    timeout_seconds: Mapped[float] = mapped_column(default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SiteModelProvider(Base):
    __tablename__ = "site_model_providers"
    __table_args__ = (UniqueConstraint("connection_id", "model", name="uq_connection_model"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("model_connections.id"), index=True)
    chat_protocol: Mapped[str] = mapped_column(String(30), default="chat_completions", server_default="chat_completions")
    supports_chat: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    image_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    model: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    thinking_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    thinking_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    supports_images: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    context_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prices: Mapped[dict] = mapped_column(JSON, default=dict)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ThinkingTest(Base):
    __tablename__ = "model_thinking_tests"
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("site_model_providers.id"))
    version: Mapped[int] = mapped_column(Integer)
    connection_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    thinking_version: Mapped[int] = mapped_column(Integer)
    option_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImageModelSettings(Base):
    __tablename__ = "image_model_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    generate_model_id: Mapped[UUID | None] = mapped_column(ForeignKey("site_model_providers.id"), nullable=True)
    edit_model_id: Mapped[UUID | None] = mapped_column(ForeignKey("site_model_providers.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ImageTask(Base):
    __tablename__ = "image_tasks"
    __table_args__ = (UniqueConstraint("workspace_id", "actor_user_id", "idempotency_key", name="uq_image_task_request"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    model_id: Mapped[UUID] = mapped_column(ForeignKey("site_model_providers.id"))
    session_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    held_credits: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    charged_credits: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    upstream_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    outputs: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LLMProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_llm_provider_workspace_key"),
        UniqueConstraint("workspace_id", "site_provider_id", name="uq_workspace_site_provider"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    site_provider_id: Mapped[UUID | None] = mapped_column(ForeignKey("site_model_providers.id"), nullable=True, index=True)
    source_price_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    credits_per_unit: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
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
    credits: Mapped[Decimal] = mapped_column(Numeric(24, 4))
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
    default_provider_id: Mapped[UUID | None] = mapped_column(ForeignKey("site_model_providers.id"), nullable=True)
    connections_migrated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    shared_pricing_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    pricing_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class BillingSnapshot(Base):
    __tablename__ = "billing_snapshots"
    __table_args__ = (UniqueConstraint("workspace_id", "request_id", "event_key",
                                      name="uq_billing_snapshot_operation"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    event_key: Mapped[str] = mapped_column(String(100))
    context: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelUsageRequest(Base):
    __tablename__ = "model_usage_requests"
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    hold: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    credits: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    formula_version: Mapped[str] = mapped_column(String(32), default="model_request_v2")
    billing_context: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
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


class SearchToolCall(Base):
    __tablename__ = "search_tool_calls"
    __table_args__ = (UniqueConstraint("request_id", "call_key", name="uq_search_tool_call"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("model_usage_requests.request_id"), index=True)
    call_key: Mapped[str] = mapped_column(String(160))
    capability: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="started")
    units: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[dict] = mapped_column(JSON)
    encrypted_result: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    provider_key: Mapped[str] = mapped_column(String(40), default="zhifux", server_default="zhifux")
    payment_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recharge_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payment_method_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    quote: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="creating")
    platform_order_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pay_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class SitePaymentConfig(Base):
    __tablename__ = "site_payment_configs"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(40), default="zhifux")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    api_base_url: Mapped[str] = mapped_column(String(500), default="")
    merchant_num: Mapped[str] = mapped_column(String(100), default="")
    pay_type: Mapped[str] = mapped_column(String(100), default="")
    payment_methods: Mapped[list[dict]] = mapped_column(JSON, default=list)
    custom_credits_per_yuan: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    packages: Mapped[list[dict]] = mapped_column(JSON, default=list)
    custom_recharge_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SiteRechargeConfig(Base):
    __tablename__ = "site_recharge_configs"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    packages: Mapped[list[dict]] = mapped_column(JSON, default=list)
    custom_recharge_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    custom_credits_per_yuan: Mapped[int] = mapped_column(Integer, default=100)
    payment_methods: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    content_origin: Mapped[str] = mapped_column(String(30), default="uploaded", server_default="uploaded")
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


class ConversationDocumentSave(Base):
    __tablename__ = "conversation_document_saves"
    __table_args__ = (UniqueConstraint("user_id", "request_id", "knowledge_base_id", "content_hash", name="uq_conversation_document_save"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[UUID] = mapped_column(ForeignKey("chat_messages.id"))
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    knowledge_base_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_bases.id"))
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    source_citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    dispatch_pending: Mapped[bool] = mapped_column(default=True, server_default="true", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    extensions_initialized: Mapped[bool] = mapped_column(default=True)
    question_workbench: Mapped[bool] = mapped_column(default=False, server_default="false")
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)
    active_request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)
    libraries: Mapped[list["ConversationLibrary"]] = relationship(cascade="all, delete-orphan")
    messages: Mapped[list["ChatMessage"]] = relationship(cascade="all, delete-orphan", order_by="ChatMessage.sequence")


class QuestionWorkbenchGrant(Base):
    __tablename__ = "question_workbench_grants"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=1)


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
    document_mentions: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, unique=True)
    provider_id: Mapped[UUID | None] = mapped_column(ForeignKey("llm_provider_configs.id"), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider: Mapped["LLMProviderConfig | None"] = relationship(lazy="selectin")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    thinking_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    encrypted_continuation: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_display_thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    thinking_diagnostics: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    web_sources: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    traces: Mapped[list[dict]] = mapped_column(JSON, default=list)
    tool_events: Mapped[list[dict]] = mapped_column(JSON, default=list)
    display_blocks: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    retrieval_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    refused: Mapped[bool] = mapped_column(default=False)
    refusal_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    feedback: Mapped["AnswerFeedback | None"] = relationship(
        uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    attachments: Mapped[list["ChatAttachment"]] = relationship(
        secondary=chat_message_resources, lazy="selectin",
        order_by="(chat_message_resources.c.position, ChatAttachment.created_at, ChatAttachment.id)",
    )


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(24), default="chat_upload", server_default="chat_upload")
    resource_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resource_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("workspace_files.id"), nullable=True, index=True)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    over_quota_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    object_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceFile(Base):
    __tablename__ = "workspace_files"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="uq_workspace_file_path"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(24), default="model_output", server_default="model_output")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    versions: Mapped[list["WorkspaceFileVersion"]] = relationship(cascade="all, delete-orphan", order_by="WorkspaceFileVersion.version")


class WorkspaceFileVersion(Base):
    __tablename__ = "workspace_file_versions"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_workspace_file_version"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(ForeignKey("workspace_files.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(700), unique=True)
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)


class WorkspaceExecutionJob(Base):
    __tablename__ = "workspace_execution_jobs"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    request_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    command_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_summary: Mapped[str] = mapped_column(Text, default="")
    stderr_summary: Mapped[str] = mapped_column(Text, default="")
    changed_files: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContextCheckpoint(Base):
    __tablename__ = "conversation_context_checkpoints"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    through_sequence: Mapped[int] = mapped_column(Integer)
    source_digest: Mapped[str] = mapped_column(String(64))
    scope_digest: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("llm_provider_configs.id"))
    model: Mapped[str] = mapped_column(String(200))
    estimated_before: Mapped[int] = mapped_column(Integer)
    estimated_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextOperation(Base):
    __tablename__ = "conversation_context_operations"
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class SiteSkill(Base):
    __tablename__ = "site_skills"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    markdown: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    files: Mapped[list["SiteSkillFile"]] = relationship(cascade="all, delete-orphan")


class SiteSkillFile(Base):
    __tablename__ = "site_skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("site_skills.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[bytes] = mapped_column(LargeBinary)


class ProductSuggestion(Base):
    __tablename__ = "product_suggestions"
    __table_args__ = (UniqueConstraint("user_id", "request_id", name="uq_suggestion_request"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    category: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    handled_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentSkill(Base):
    __tablename__ = "agent_skills"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    markdown: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    limitations_acknowledged: Mapped[bool] = mapped_column(default=False)
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
    version: Mapped[int] = mapped_column(default=1)
    authorization_version: Mapped[int] = mapped_column(default=1)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class CloudComputerSettings(Base):
    __tablename__ = "cloud_computer_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)


class CloudComputerSession(Base):
    __tablename__ = "cloud_computer_sessions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    session_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="starting", index=True)
    provider_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    held_credits: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    charged_credits: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    billed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    billing_sequence: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    billing_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("uq_cloud_computer_active_user", "user_id", unique=True,
        postgresql_where=ended_at.is_(None), sqlite_where=ended_at.is_(None)),)


class CloudComputerCommand(Base):
    __tablename__ = "cloud_computer_commands"
    __table_args__ = (UniqueConstraint("session_id", "request_id", "digest", name="uq_cloud_command_request"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("cloud_computer_sessions.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    process_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RemoteToolExecution(Base):
    __tablename__ = "remote_tool_executions"
    __table_args__ = (UniqueConstraint("message_id", "tool_name", "arguments_digest"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    tool_name: Mapped[str] = mapped_column(String(64))
    authorization_version: Mapped[int] = mapped_column(Integer)
    arguments_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running")
    encrypted_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

