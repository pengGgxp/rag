"""Workspace-wide capacity checks and prepaid, independently expiring allowances."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text

from .auth import Access, aware, get_access, now
from .audit_log import record_audit
from .credit_amounts import CreditRate
from .credits import _entry, ensure_account
from .db import get_db
from .models import AgentSkill, ChatAttachment, Document, DocumentUpload, KnowledgeBase, Workspace, WorkspaceFile
from .site_auth import site_admin
from .site_audit import record
from .storage_models import StorageEntitlement, StorageQuotaSettings, StorageTransfer, WorkspaceStorageState

router = APIRouter(tags=["storage-allowances"])
MIB, GIB = 1024**2, 1024**3
BEIJING = timezone(timedelta(hours=8))
ByteCount = Annotated[int, Field(ge=0, le=1024**5, strict=True)]
DEFAULT_PACKAGES = [
    {"id": key, "name": name, "storage_bytes": storage * GIB, "download_bytes": download * GIB,
     "days": 30, "price_credits": None, "enabled": False, "order": order}
    for order, (key, name, storage, download) in enumerate([
        ("storage-5", "5 GiB 容量包", 5, 1), ("storage-20", "20 GiB 容量包", 20, 5),
        ("storage-50", "50 GiB 容量包", 50, 10), ("download-5", "5 GiB 下载包", 0, 5)])]


class Package(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]{1,50}$")
    name: str = Field(min_length=1, max_length=80)
    storage_bytes: ByteCount
    download_bytes: ByteCount
    days: int = Field(ge=1, le=365, strict=True)
    price_credits: CreditRate | None = None
    enabled: bool = False
    order: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def valid_package(self):
        if not self.storage_bytes and not self.download_bytes:
            raise ValueError("套餐至少包含一项额度")
        if self.enabled and self.price_credits is None:
            raise ValueError("启用套餐前请填写积分价格")
        return self


class CostInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_yuan_gib_month: str = Field(default="0.118", pattern=r"^\d{1,6}(?:\.\d{1,8})?$")
    egress_yuan_gib: str = Field(default="0.5", pattern=r"^\d{1,6}(?:\.\d{1,8})?$")
    reads_yuan_10000: str = Field(default="0.01", pattern=r"^\d{1,6}(?:\.\d{1,8})?$")
    writes_yuan_10000: str = Field(default="0.01", pattern=r"^\d{1,6}(?:\.\d{1,8})?$")
    transfer_yuan_gib: str = Field(default="0", pattern=r"^\d{1,6}(?:\.\d{1,8})?$")


class QuotaWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    free_storage_bytes: ByteCount = 512 * MIB
    free_download_bytes: ByteCount = 512 * MIB
    output_limit_bytes: ByteCount = 512 * MIB
    retention_days: int = Field(ge=1, le=30, strict=True)
    packages: list[Package] = Field(max_length=30)
    costs: CostInputs = Field(default_factory=CostInputs)

    @model_validator(mode="after")
    def unique_packages(self):
        if len({p.id for p in self.packages}) != len(self.packages):
            raise ValueError("套餐标识不能重复")
        return self


def settings(db, *, lock=False):
    if lock and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020072)"))
    row = db.get(StorageQuotaSettings, 1, with_for_update=lock, populate_existing=lock)
    if row is None:
        # Reads do not create configuration or make packages billable.
        return StorageQuotaSettings(id=1, version=0, free_storage_bytes=512*MIB,
            free_download_bytes=512*MIB, output_limit_bytes=512*MIB, retention_days=3,
            packages=DEFAULT_PACKAGES, costs=CostInputs().model_dump())
    return row


def lock_workspace(db, identifier):
    if db.get(Workspace, identifier, with_for_update=True, populate_existing=True) is None:
        raise HTTPException(404, "工作区不存在")


def inventory(db, identifier, *, exclude_key=None, exclude_keys=()):
    """Count each retained object once; mounted resources have one current key."""
    excluded = set(exclude_keys)
    if exclude_key:
        excluded.add(exclude_key)
    objects = {}
    groups = {"documents": 0, "attachments": 0, "outputs": 0, "skills": 0, "reserved": 0}
    sources = [
        ("documents", db.execute(select(Document.object_key, Document.size_bytes).join(KnowledgeBase)
            .where(KnowledgeBase.workspace_id == identifier))),
        ("attachments", db.execute(select(ChatAttachment.object_key, ChatAttachment.size_bytes)
            .where(ChatAttachment.workspace_id == identifier, ChatAttachment.object_deleted_at.is_(None),
                ChatAttachment.resource_file_id.is_(None)))),
    ]
    from .shared_resources import object_key
    from .shared_resources import backend, prefix
    mounted = backend(db)
    if mounted is not None:
        sources.append(("outputs", ((item["Key"], int(item["Size"]))
            for item in mounted.iter_objects(prefix(identifier))
            if not item["Key"].endswith("/") and item["Key"].rsplit("/", 1)[-1] != ".rag-directory")))
    else:
        sources.append(("outputs", ((object_key(row.workspace_id, row.path), row.size_bytes)
            for row in db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == identifier,
                WorkspaceFile.deleted_at.is_(None))))))
    reserved = db.execute(
        select(DocumentUpload.object_key, DocumentUpload.size_bytes).where(
            DocumentUpload.workspace_id == identifier,
            DocumentUpload.state.not_in(["cleaned", "cancelled", "completed"]),
        )
    )
    sources.append(("reserved", reserved))
    for group, rows in sources:
        for key, size in rows:
            if key not in excluded and key not in objects:
                objects[key] = size
                groups[group] += size
    for row in db.scalars(select(AgentSkill).where(AgentSkill.workspace_id == identifier)):
        groups["skills"] += len(row.markdown.encode("utf-8")) + sum(len(f.content) for f in row.files)
    return {"used_bytes": sum(groups.values()), "breakdown": groups}


def active_entitlements(db, identifier, at=None):
    return list(db.scalars(select(StorageEntitlement).where(StorageEntitlement.workspace_id == identifier,
        StorageEntitlement.expires_at > (at or now())).order_by(StorageEntitlement.expires_at,
            StorageEntitlement.kind, StorageEntitlement.id)))


def storage_limit(db, identifier, config=None):
    return (config or settings(db)).free_storage_bytes + sum(e.storage_bytes for e in active_entitlements(db, identifier))


def check_capacity(db, identifier, additional, *, exclude_key=None, exclude_keys=(),
                   skill_replaced_bytes=0, output=False):
    lock_workspace(db, identifier)
    config = settings(db)
    usage = inventory(db, identifier, exclude_key=exclude_key, exclude_keys=exclude_keys)
    if usage["used_bytes"] - skill_replaced_bytes + additional > storage_limit(db, identifier, config):
        raise HTTPException(413, "工作区存储空间不足，请联系工作区管理员增加额度")
    state = db.get(WorkspaceStorageState, identifier)
    if state is not None:
        state.over_quota_since = None
    return config


def month_bounds(at):
    local = at.astimezone(BEIJING)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def free_month(db, identifier):
    start, end = month_bounds(now())
    key = f"free:{start:%Y-%m}"
    row = db.scalar(select(StorageEntitlement).where(StorageEntitlement.workspace_id == identifier,
        StorageEntitlement.request_key == key))
    if row is None:
        row = StorageEntitlement(workspace_id=identifier, request_key=key, kind="free", name="每月免费额度",
            download_bytes=settings(db).free_download_bytes, storage_bytes=0, expires_at=end.astimezone(timezone.utc),
            snapshot={}, used_bytes=0, reserved_bytes=0, price_credits=0)
        db.add(row)
        db.flush()
    else:
        # Changing the free allowance preserves consumption and already reserved transfers.
        row.download_bytes = settings(db).free_download_bytes
    return row


def finish_transfer(db, transfer, status):
    if transfer.status != "active":
        return
    # Only acknowledged ASGI sends consume allowance. Crash-window bytes remain uncertain and free.
    for part in transfer.allocations:
        grant = db.get(StorageEntitlement, UUID(part["id"]))
        grant.reserved_bytes -= part["reserved"]
    transfer.uncertain_bytes += transfer.inflight_bytes
    transfer.inflight_bytes = 0
    transfer.status = status


def recover_transfers(db, identifier):
    for transfer in db.scalars(select(StorageTransfer).where(StorageTransfer.workspace_id == identifier,
            StorageTransfer.status == "active", StorageTransfer.lease_until <= now())):
        finish_transfer(db, transfer, "interrupted")


def reserve_download(db, access, size, resource):
    lock_workspace(db, access.workspace_id)
    recover_transfers(db, access.workspace_id)
    free_month(db, access.workspace_id)
    grants = active_entitlements(db, access.workspace_id)
    if sum(max(0, e.download_bytes-e.used_bytes-e.reserved_bytes) for e in grants) < size:
        raise HTTPException(402, "工作区下载额度不足，请联系工作区管理员增加额度")
    left, allocations = size, []
    for grant in grants:
        count = min(left, max(0, grant.download_bytes-grant.used_bytes-grant.reserved_bytes))
        if count:
            grant.reserved_bytes += count
            allocations.append({"id": str(grant.id), "reserved": count})
            left -= count
    row = StorageTransfer(workspace_id=access.workspace_id, user_id=access.user_id, resource=resource,
        requested_bytes=size, allocations=allocations, lease_until=now()+timedelta(minutes=2),
        sent_bytes=0, inflight_bytes=0, uncertain_bytes=0, status="active")
    db.add(row)
    db.flush()
    return row.id


def transfer_checkpoint(db, identifier, *, preparing=0, sent=0, finish=None):
    row = db.get(StorageTransfer, identifier)
    if row is None:
        raise RuntimeError("Missing transfer reservation")
    lock_workspace(db, row.workspace_id)
    db.refresh(row)
    if row.status != "active":
        if finish == row.status and not preparing and not sent:
            return
        raise RuntimeError("Transfer reservation expired")
    if sent:
        if sent != row.inflight_bytes:
            raise RuntimeError("Invalid transfer checkpoint")
        left, parts = sent, []
        for part in row.allocations:
            grant = db.get(StorageEntitlement, UUID(part["id"]))
            count = min(left, part["reserved"])
            grant.reserved_bytes -= count
            grant.used_bytes += count
            parts.append({**part, "reserved": part["reserved"]-count})
            left -= count
        if left:
            raise RuntimeError("Transfer exceeded its reservation")
        row.allocations = parts
        row.sent_bytes += sent
        row.inflight_bytes = 0
    if preparing:
        if row.inflight_bytes or preparing > row.requested_bytes-row.sent_bytes:
            raise RuntimeError("Invalid transfer size")
        row.inflight_bytes = preparing
    row.lease_until = now()+timedelta(minutes=2)
    if finish:
        finish_transfer(db, row, finish)


def entitlement_read(row):
    return {"id": str(row.id), "name": row.name, "kind": row.kind, "storage_bytes": row.storage_bytes,
        "download_bytes": row.download_bytes, "remaining_bytes": max(0, row.download_bytes-row.used_bytes-row.reserved_bytes),
        "price_credits": str(row.price_credits), "created_at": row.created_at, "expires_at": row.expires_at}


def workspace_read(db, identifier):
    lock_workspace(db, identifier)
    recover_transfers(db, identifier)
    free_month(db, identifier)
    usage = inventory(db, identifier)
    config = settings(db)
    grants = active_entitlements(db, identifier)
    limit = storage_limit(db, identifier, config)
    state = db.get(WorkspaceStorageState, identifier)
    if state is None:
        state = WorkspaceStorageState(workspace_id=identifier)
        db.add(state)
    state.over_quota_since = (state.over_quota_since or now()) if usage["used_bytes"] > limit else None
    history = db.scalars(select(StorageEntitlement).where(StorageEntitlement.workspace_id == identifier,
        StorageEntitlement.kind != "free").order_by(StorageEntitlement.created_at.desc()).limit(100)).all()
    transfers = list(db.execute(select(StorageTransfer.resource, func.sum(StorageTransfer.sent_bytes)).where(
        StorageTransfer.workspace_id == identifier, StorageTransfer.created_at >= month_bounds(now())[0])
        .group_by(StorageTransfer.resource)))
    result = {**usage, "limit_bytes": limit,
        "download_remaining_bytes": sum(max(0, e.download_bytes-e.used_bytes-e.reserved_bytes) for e in grants),
        "download_resets_at": month_bounds(now())[1], "retention_days": None,
        "over_quota_since": state.over_quota_since,
        "download_usage": {"model_read": sum(size for resource, size in transfers if resource.startswith("model_read:")),
            "user_download": sum(size for resource, size in transfers if not resource.startswith("model_read:"))},
        "cleanup_due_at": aware(state.over_quota_since)+timedelta(days=30) if state.over_quota_since else None,
        "entitlements": [entitlement_read(e) for e in grants if e.kind != "free"],
        "history": [entitlement_read(e) for e in history], "config_version": config.version,
        "packages": [p for p in sorted(config.packages, key=lambda p: p["order"]) if p["enabled"]]}
    db.commit()
    return result


def config_read(row):
    return {key: getattr(row, key) for key in ("version", "free_storage_bytes", "free_download_bytes",
        "output_limit_bytes", "retention_days", "packages", "costs")}


@router.get("/site/admin/storage/quotas")
def get_config(db=Depends(get_db), actor=Depends(site_admin)):
    return config_read(settings(db))


@router.put("/site/admin/storage/quotas")
def put_config(payload: QuotaWrite, db=Depends(get_db), actor=Depends(site_admin)):
    row = settings(db, lock=True)
    if row.version != payload.version:
        raise HTTPException(409, "额度与套餐已更新，请明确放弃修改后重新加载")
    for key, value in payload.model_dump(mode="json", exclude={"version"}).items():
        setattr(row, key, value)
    row.version += 1
    db.add(row)
    record(db, actor, "storage.quotas_updated", "storage", 1, details={"version": row.version})
    db.commit()
    return config_read(row)


@router.get("/storage/allowances")
def get_allowances(db=Depends(get_db), access: Access=Depends(get_access)):
    return workspace_read(db, access.workspace_id)


class Purchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_id: str = Field(max_length=50)
    version: int = Field(ge=1)
    request_id: UUID


def purchase(db, access, payload, request=None):
    if access.role != "admin":
        raise HTTPException(403, "只有工作区管理员可以购买额度")
    config = settings(db, lock=True)
    lock_workspace(db, access.workspace_id)
    key = f"purchase:{payload.request_id}"
    existing = db.scalar(select(StorageEntitlement).where(StorageEntitlement.workspace_id == access.workspace_id,
        StorageEntitlement.request_key == key))
    if existing:
        if existing.snapshot.get("package_id") != payload.package_id or existing.snapshot.get("version") != payload.version:
            raise HTTPException(409, "请为新的购买操作使用新的请求")
        return entitlement_read(existing)
    if config.version != payload.version:
        raise HTTPException(409, "套餐已更新，请重新查看后购买")
    package = next((p for p in config.packages if p["id"] == payload.package_id and p["enabled"]), None)
    if not package or package["price_credits"] is None:
        raise HTTPException(409, "此套餐暂不可购买")
    price = Decimal(str(package["price_credits"]))
    account = ensure_account(db, access.workspace_id, initial=0)
    row = StorageEntitlement(workspace_id=access.workspace_id, actor_user_id=access.user_id, request_key=key,
        kind="purchase", name=package["name"], storage_bytes=package["storage_bytes"],
        download_bytes=package["download_bytes"], price_credits=price,
        expires_at=now()+timedelta(days=package["days"]),
        snapshot={**package, "package_id": payload.package_id, "version": config.version})
    db.add(row)
    db.flush()
    _entry(db, account, actor_user_id=access.user_id, amount=-price, kind="storage_package_purchase",
        idempotency_key=f"storage:{access.workspace_id}:{payload.request_id}",
        metadata={"entitlement_id": str(row.id), "name": row.name, "package_id": payload.package_id})
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="storage.purchased", resource_type="storage_entitlement", resource_id=row.id, request=request,
        metadata={"credits": str(price), "package_id": payload.package_id})
    db.commit()
    return entitlement_read(row)


@router.post("/storage/purchases", status_code=201)
def buy(payload: Purchase, request: Request, db=Depends(get_db), access=Depends(get_access)):
    try:
        return purchase(db, access, payload, request)
    except BaseException:
        db.rollback()
        raise


@router.get("/site/admin/workspaces/{workspace_id}/storage")
def admin_workspace(workspace_id: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    return workspace_read(db, workspace_id)


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_bytes: ByteCount
    download_bytes: ByteCount
    days: int = Field(ge=1, le=365, strict=True)
    reason: str = Field(min_length=1, max_length=200)
    request_id: UUID


@router.post("/site/admin/workspaces/{workspace_id}/storage/grants", status_code=201)
def grant(workspace_id: UUID, payload: Grant, db=Depends(get_db), actor=Depends(site_admin)):
    lock_workspace(db, workspace_id)
    key = f"grant:{payload.request_id}"
    row = db.scalar(select(StorageEntitlement).where(StorageEntitlement.workspace_id == workspace_id,
        StorageEntitlement.request_key == key))
    snapshot = payload.model_dump(mode="json")
    if row and row.snapshot != snapshot:
        raise HTTPException(409, "该操作已用于其他调整，请重新提交")
    if row is None:
        if not payload.storage_bytes and not payload.download_bytes:
            raise HTTPException(422, "请填写需要增加的额度")
        row = StorageEntitlement(workspace_id=workspace_id, actor_user_id=actor.id, request_key=key,
            kind="grant", name="管理员调整", storage_bytes=payload.storage_bytes,
            download_bytes=payload.download_bytes, expires_at=now()+timedelta(days=payload.days), snapshot=snapshot)
        db.add(row)
        db.flush()
        record(db, actor, "storage.granted", "workspace", workspace_id,
            details={"resource_id": str(row.id), "amount": {"storage_bytes": payload.storage_bytes,
                "download_bytes": payload.download_bytes, "days": payload.days}, "request_id": str(payload.request_id)})
        db.commit()
    return entitlement_read(row)


@router.get("/site/admin/storage/workspaces")
def workspace_list(page: int=Query(1, ge=1), db=Depends(get_db), actor=Depends(site_admin)):
    rows = db.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.id).offset((page-1)*25).limit(26)).all()
    return {"items": [{"id": str(row.id), "name": row.name, **workspace_read(db, row.id)} for row in rows[:25]],
        "page": page, "has_more": len(rows) > 25}


def maintenance(db):
    from .models import ChatAttachment, WorkspaceFile
    for identifier in list(db.scalars(select(Workspace.id))):
        state = workspace_read(db, identifier)
        if not state["over_quota_since"] or aware(state["over_quota_since"])+timedelta(days=30) > now():
            continue
        lock_workspace(db, identifier)
        current_state = db.get(WorkspaceStorageState, identifier, populate_existing=True)
        if (current_state is None or current_state.over_quota_since is None
                or aware(current_state.over_quota_since)+timedelta(days=30) > now()):
            db.rollback()
            continue
        config = settings(db)
        limit = storage_limit(db, identifier, config)
        candidates = []
        for row in db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == identifier,
                WorkspaceFile.deleted_at.is_(None)).order_by(WorkspaceFile.created_at, WorkspaceFile.id)):
            candidates.append((row.created_at, str(row.id), "file", row, row.size_bytes))
        for row in db.scalars(select(ChatAttachment).where(ChatAttachment.workspace_id == identifier,
                ChatAttachment.resource_file_id.is_(None), ChatAttachment.deleted_at.is_(None), ChatAttachment.over_quota_deleted_at.is_(None))
                .order_by(ChatAttachment.created_at, ChatAttachment.id)):
            candidates.append((row.created_at, str(row.id), "attachment", row, row.size_bytes))
        for _, _, kind, row, _ in sorted(candidates, key=lambda item: (aware(item[0]), item[1])):
            if inventory(db, identifier)["used_bytes"] <= limit:
                break
            if kind == "file":
                from .shared_resources import remove_live
                try:
                    remove_live(db, row)
                except Exception:
                    db.rollback()
                    continue
                row.deleted_at = now()
                for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id)):
                    attachment.deleted_at = row.deleted_at
                    attachment.object_deleted_at = row.deleted_at
            else:
                row.over_quota_deleted_at = now()
                row.deleted_at = now()
                try:
                    if not db.scalar(select(Document.id).where(Document.object_key == row.object_key)):
                        from .storage import delete_object
                        delete_object(row.object_key)
                        row.object_deleted_at = now()
                except Exception:
                    pass
            record_audit(db, action="resource.auto_deleted", workspace_id=identifier,
                resource_type="resource", resource_id=row.id, metadata={"reason": "storage_over_quota"})
            db.flush()
        db.commit()
