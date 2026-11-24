"""Prepaid storage entitlements and recoverable transfer reservations."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db import Base


class StorageQuotaSettings(Base):
    __tablename__ = "storage_quota_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=0)
    free_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=512 * 1024**2)
    free_download_bytes: Mapped[int] = mapped_column(BigInteger, default=512 * 1024**2)
    output_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=512 * 1024**2)
    retention_days: Mapped[int] = mapped_column(default=3)
    packages: Mapped[list] = mapped_column(JSON, default=list)
    costs: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceStorageState(Base):
    __tablename__ = "workspace_storage_states"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    over_quota_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageEntitlement(Base):
    __tablename__ = "storage_entitlements"
    __table_args__ = (UniqueConstraint("account_id", "request_key", name="uq_storage_entitlement_request"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    request_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(80))
    storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    download_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    price_credits: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StorageTransfer(Base):
    __tablename__ = "storage_transfers"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    resource: Mapped[str] = mapped_column(String(200))
    requested_bytes: Mapped[int] = mapped_column(BigInteger)
    sent_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uncertain_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    inflight_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    allocations: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
