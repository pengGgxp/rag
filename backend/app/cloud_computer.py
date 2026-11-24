"""Durable cloud execution, resource synchronization and metered session lifetime."""
import hashlib
import json
import logging
import math
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from contextlib import contextmanager
import threading
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import func, select, text, update

from .auth import Access, aware, get_access, now
from .credit_amounts import CreditRate, amount
from .credits import CreditError, _entry, authorization_hold, ensure_account, release_hold
from .db import SessionLocal, get_db
from .models import CloudComputerCommand as Command, CloudComputerSession as Computer, CloudComputerSettings as Settings, Conversation, UsageEvent
from .site_auth import protect, reveal, site_admin
from .cloud_computer_adapter import adapter

router = APIRouter(tags=["cloud-computer"])
logger = logging.getLogger(__name__)
ACTIVE = ("starting", "ready", "running", "stopping", "uncertain")
BUSY = ("queued", "preparing", "running")

STOP_REASONS = {
    "user_requested": "你已请求结束云电脑",
    "idle_timeout": "长时间无操作",
    "max_lifetime": "已达到最长运行时间",
    "insufficient_balance": "可用积分不足以继续运行",
    "reservation_expired": "运行状态核对中断",
    "access_revoked": "访问权限已失效",
    "execution_interrupted": "任务执行中断",
    "provider_ended": "运行环境已停止",
    "startup_failed": "云电脑未能启动",
}


class ReconcileResult(Enum):
    CONTINUE = "continue"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RESERVATION_EXPIRED = "reservation_expired"
    STOPPED = "stopped"


def set_stop_reason(db, row, reason):
    # A manual end and maintenance can race; preserve the first persisted cause.
    db.execute(update(Computer).where(Computer.id == row.id, Computer.stop_reason.is_(None))
        .values(stop_reason=reason).execution_options(synchronize_session=False))
    db.refresh(row, ["stop_reason"])


def renewal_reserve(row):
    return amount(Decimal(str(row.snapshot["credits_per_minute"])) * row.snapshot["idle_seconds"] / 60)


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    region: str = Field(default="", max_length=60, pattern=r"^([a-z0-9]+(-[a-z0-9]+)*)?$")
    template: str = Field(default="", max_length=200, pattern=r"^[a-zA-Z0-9._/-]*$")
    specification: str = Field(default="", max_length=120)
    credits_per_minute: CreditRate | None = Decimal("1")
    idle_seconds: int = Field(default=600, ge=60, le=1800)
    max_seconds: int = Field(default=3600, ge=300, le=3600)
    blocked_hosts: list[str] = Field(default_factory=list, max_length=100)
    oss_role_arn: str = Field(default="", max_length=256,
        pattern=r"^(acs:ram::[0-9]+:role/[A-Za-z0-9._/-]+)?$")

    @model_validator(mode="after")
    def valid(self):
        import ipaddress
        import re
        if self.idle_seconds > self.max_seconds:
            raise ValueError("闲置时间不能超过最长运行时间")
        for host in self.blocked_hosts:
            try:
                ipaddress.ip_network(host, strict=False)
            except ValueError:
                if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
                    raise ValueError("禁止访问地址只能填写域名、IP 或网段")
        return self


class SettingsInput(Configuration):
    version: int = Field(ge=0)
    enabled: bool
    api_key: SecretStr | None = None
    clear_api_key: bool = False


def settings_read(row):
    return {"version": row.version if row else 0, "enabled": bool(row and row.enabled),
        "has_api_key": bool(row and row.encrypted_api_key),
        **Configuration.model_validate(row.config if row else {}).model_dump(mode="json")}


def serialize_admission(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(3020066)"))


@router.get("/site/admin/cloud-computer")
def get_settings(db=Depends(get_db), actor=Depends(site_admin)):
    return settings_read(db.get(Settings, 1))


@router.put("/site/admin/cloud-computer")
def save_settings(payload: SettingsInput, request: Request, db=Depends(get_db), actor=Depends(site_admin)):
    from .site_audit import record
    serialize_admission(db)
    row = db.get(Settings, 1, with_for_update=True, populate_existing=True)
    if payload.version != (row.version if row else 0):
        raise HTTPException(409, "云电脑设置已更新，请重新加载后再保存")
    before = settings_read(row)
    encrypted = row.encrypted_api_key if row else None
    if payload.api_key and payload.clear_api_key:
        raise HTTPException(422, "不能同时填写和清除密钥")
    if payload.clear_api_key:
        encrypted = None
    if payload.api_key:
        encrypted = protect(payload.api_key.get_secret_value())
    config = Configuration.model_validate(
        payload.model_dump(include=set(Configuration.model_fields))).model_dump(mode="json")
    if payload.enabled:
        if not encrypted or not all(config[key] for key in ("region", "template", "specification")) or config["credits_per_minute"] is None:
            raise HTTPException(422, "请先填写地域、模板、规格、密钥及积分单价")
        reveal(encrypted)
    if row is None:
        row = Settings(id=1, version=0)
        db.add(row)
    row.enabled, row.config, row.encrypted_api_key = payload.enabled, config, encrypted
    row.version += 1
    fields = [key for key, value in config.items() if before.get(key) != value]
    fields += [key for key in ("enabled",) if before[key] != getattr(payload, key)]
    if payload.api_key or payload.clear_api_key:
        fields.append("api_key")
    record(db, actor, "cloud_computer.settings_updated", "cloud_computer_settings", 1,
           request=request, details={"fields": fields, "version": row.version})
    db.commit()
    return settings_read(row)


@router.post("/site/admin/cloud-computer/check")
def check_connection(db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(Settings, 1)
    if not row or not row.encrypted_api_key or not row.config.get("region"):
        raise HTTPException(422, "请先保存地域和密钥")
    try:
        # List is deliberately non-creating; this does not test the paid template.
        adapter(row.config, row.encrypted_api_key).find(uuid4())
    except Exception:
        logger.warning("Cloud computer connection check failed", exc_info=False)
        raise HTTPException(503, "连接未能验证，请检查地域和密钥后重试")
    return {"connected": True, "template_verified": False}


def owner(db, access, conversation_id):
    access = access.revalidate(db)
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.deleted_at:
        raise HTTPException(404, "会话不存在")
    access.conversation(db, conversation)
    return access


def session_access(db, row):
    from .models import User
    user = db.get(User, row.user_id, populate_existing=True)
    if user is None or user.is_suspended:
        raise HTTPException(403, "云电脑访问权限已失效")
    return Access(row.user_id, row.session_hash)


def owned_computer(db, access, identifier, *, write=False):
    access = access.revalidate(db)
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError):
        raise HTTPException(422, "请指定有效的 computer_id") from None
    row = db.get(Computer, identifier, populate_existing=True)
    if row is None or row.user_id != access.user_id:
        raise HTTPException(404, "云电脑不存在或无权访问")
    return row


def charge_at(row, end):
    if not row.started_at:
        return 0, Decimal(0)
    end = min(aware(end), aware(row.started_at) + timedelta(seconds=row.snapshot["max_seconds"]))
    if row.billing_stopped_at:
        end = min(end, aware(row.billing_stopped_at))
    seconds = max(0, math.floor((end - aware(row.started_at)).total_seconds()))
    rate = Decimal(str(row.snapshot["credits_per_minute"]))
    return seconds, amount(rate * seconds / 60)


def reconcile(db, row, end, final=False) -> ReconcileResult:
    """Round cumulative usage once; intervals book only the cumulative delta."""
    result = ReconcileResult.STOPPED if final or row.billing_stopped_at else ReconcileResult.CONTINUE
    seconds, total = charge_at(row, end)
    limit = row.charged_credits + row.held_credits
    if total > limit:
        rate = Decimal(str(row.snapshot["credits_per_minute"]))
        seconds = int((limit * 60 / rate).to_integral_value(rounding=ROUND_FLOOR)) if rate else seconds
        if not row.billing_stopped_at:
            result = ReconcileResult.RESERVATION_EXPIRED
            set_stop_reason(db, row, "reservation_expired")
        row.billing_stopped_at = aware(row.started_at) + timedelta(seconds=seconds)
        total = amount(rate * seconds / 60)
    delta = max(Decimal(0), total - row.charged_credits)
    if row.held_credits:
        release_hold(db, row.account_id, row.user_id, uuid5(row.id, f"hold:{row.billing_sequence}"), row.held_credits)
    row.held_credits = Decimal(0)
    if delta:
        _entry(db, ensure_account(db, row.account_id), actor_user_id=row.user_id, amount=-delta,
            kind="usage_settlement", idempotency_key=f"cloud:{row.id}:charge:{row.billing_sequence}",
            metadata={"capability": "cloud_computer", "computer_id": str(row.id), "seconds": seconds - row.billed_seconds})
        db.add(UsageEvent(account_id=row.account_id, actor_user_id=row.user_id, request_id=row.id,
            capability="cloud_computer", units=max(0, seconds-row.billed_seconds), credits=delta,
            policy_version=row.snapshot["version"], policy_snapshot={k: v for k, v in row.snapshot.items() if k != "blocked_hosts"},
            idempotency_key=f"cloud:{row.id}:usage:{row.billing_sequence}", metadata_json={"computer_id": str(row.id)}))
    row.charged_credits, row.billed_seconds = total, seconds
    row.billing_sequence += 1
    if not final and not row.billing_stopped_at:
        reserve = renewal_reserve(row)
        account = ensure_account(db, row.account_id)
        if account.balance < reserve:
            row.billing_stopped_at = end
            set_stop_reason(db, row, "insufficient_balance")
            return ReconcileResult.INSUFFICIENT_BALANCE
        if reserve:
            authorization_hold(db, row.account_id, row.user_id, uuid5(row.id, f"hold:{row.billing_sequence}"), reserve)
        row.held_credits = reserve
    return result


def read(row, db=None):
    labels = {"starting": "正在启动云电脑", "ready": "云电脑运行中", "running": "正在执行", "stopping": "正在结束云电脑",
              "uncertain": "正在核对云电脑状态", "ended": "云电脑已结束", "failed": "云电脑未能启动"}
    seconds, estimated = charge_at(row, now())
    from .cloud_preview import idle_anchor
    anchor = idle_anchor(db, row) if db is not None else aware(row.last_activity_at)
    reason_text = STOP_REASONS.get(row.stop_reason)
    if row.ended_at and row.stop_reason == "reservation_expired":
        reason_text = "运行状态核对中断，云电脑已结束"
    return {"id": str(row.id), "computer_id": str(row.id), "name": row.name or f"云电脑 {row.id.hex[:8]}",
        "conversation_id": str(row.conversation_id), "status": row.status, "summary": labels[row.status],
        "knowledge_mounted": bool(row.snapshot.get("resource_mount", {}).get("knowledge_prefix")
                                  and row.status in ("ready", "running")),
        "stop_reason": row.stop_reason, "stop_reason_text": reason_text,
        "renewal_required_credits": str(renewal_reserve(row)) if row.stop_reason == "insufficient_balance" else None,
        "terminal": row.ended_at is not None, "credits": str(row.charged_credits if row.ended_at else min(estimated, row.charged_credits+row.held_credits)),
        "credits_per_minute": str(row.snapshot["credits_per_minute"]), "seconds": seconds,
        "idle_ends_at": (anchor+timedelta(seconds=row.snapshot["idle_seconds"])).isoformat() if row.status == "ready" else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "max_ends_at": (aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"])).isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None}


@router.get("/cloud-computer/catalog")
def catalog(db=Depends(get_db), access=Depends(get_access)):
    row = db.get(Settings, 1)
    return {"available": bool(row and row.enabled), **({key: row.config[key] for key in
        ("credits_per_minute", "idle_seconds", "max_seconds")} if row and row.enabled else {})}


@router.get("/cloud-computers")
def computer_list(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  active_only: bool = False, db=Depends(get_db), access=Depends(get_access)):
    access = access.revalidate(db)
    query = select(Computer).where(Computer.user_id == access.user_id)
    if active_only:
        query = query.where(Computer.ended_at.is_(None))
    rows = db.scalars(query.order_by(Computer.ended_at.is_not(None), Computer.created_at.desc(), Computer.id)
        .offset(offset).limit(limit + 1)).all()
    return {"items": [read(row, db) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/cloud-computers/{identifier}")
def computer_detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    return {"computer": read(owned_computer(db, access, identifier), db)}


@router.post("/cloud-computers/{identifier}/end")
def end(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned_computer(db, access, identifier, write=True)
    row = db.get(Computer, row.id, with_for_update=True, populate_existing=True)
    if not row.ended_at:
        row.status = "stopping"
        row.stop_requested_at = row.stop_requested_at or now()
        row.next_reconcile_at = None
        row.billing_stopped_at = row.billing_stopped_at or now()
        set_stop_reason(db, row, "user_requested")
        for command in db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))):
            command.cancel_requested = True
        db.commit()
    return {"computer": read(row) if row else None}


def command_read(command):
    return {"id": str(command.id), "computer_id": str(command.session_id), "status": command.status, "cancel_requested": command.cancel_requested,
            "result": command.result, "terminal": command.status not in BUSY}


@router.get("/cloud-computers/{identifier}/commands")
def command_list(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned_computer(db, access, identifier)
    # Active commands are never hidden by a recent-history limit.
    rows = db.scalars(select(Command).where(Command.session_id == row.id)
        .order_by(Command.created_at.desc())).all()
    return {"items": [command_read(command) for command in rows]}


def own_command(db, access, identifier):
    command = db.get(Command, identifier, with_for_update=True)
    row = db.get(Computer, command.session_id) if command else None
    if not row or row.user_id != access.user_id:
        raise HTTPException(404, "命令不存在")
    owned_computer(db, access, row.id, write=True)
    return command, row


@router.post("/cloud-computer/commands/{identifier}/cancel")
def cancel_command(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    command, _ = own_command(db, access, identifier)
    command.cancel_requested = True
    db.commit()
    return command_read(command)


def tool_request_id(request_id, call_id):
    """Namespace the stable Agent activity key by answer ID for Command.request_id."""
    if not request_id or not isinstance(call_id, str) or not call_id.strip():
        raise HTTPException(422, "本次文件操作无法确认，请重新发送消息")
    return uuid5(UUID(str(request_id)), "cloud-tool:" + call_id)


def invocation_lock(db, identifier):
    if identifier is None:
        raise HTTPException(422, "缺少操作身份")
    if db.bind.dialect.name == "postgresql":
        key = int.from_bytes(uuid5(UUID(str(identifier)), "cloud-invocation").bytes[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


_leases = set()
_lease_guard = threading.Lock()


@contextmanager
def instance_lease(factory, identifier):
    """Nonblocking lease shared by creation and recovery; no account/global IO lock."""
    with _lease_guard:
        acquired = identifier not in _leases
        if acquired:
            _leases.add(identifier)
    if not acquired:
        yield False
        return
    try:
        with factory() as db, db.get_bind().connect() as connection:
            pg = connection.dialect.name == "postgresql"
            key = int.from_bytes(identifier.bytes[:8], "big", signed=True)
            locked = not pg or bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
            try:
                yield locked
            finally:
                if pg and locked:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        with _lease_guard:
            _leases.discard(identifier)


def create_computer(db, access, conversation_id, request_id, arguments, *,
                    agent_tool_execution_id=None, check_active=None, on_progress=None):
    access = owner(db, access, conversation_id)
    if check_active:
        check_active()
    from .agent.tool_jobs import validate_owner
    validate_owner(db, agent_tool_execution_id, access, conversation_id)
    invocation_lock(db, request_id)
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
    row = db.scalar(select(Computer).where(Computer.creation_request_id == request_id))
    if row:
        if row.user_id != access.user_id or row.creation_digest != digest:
            raise HTTPException(409, "同一次创建的内容已变化，请先核对原实例")
        return {"computer_id": str(row.id), "cloud_computer": read(row, db), "uncertain": row.status == "uncertain"}
    settings = db.get(Settings, 1, populate_existing=True)
    if not settings or not settings.enabled:
        raise HTTPException(503, "云电脑尚未开放，请联系管理员")
    from .storage_backend import active_settings
    storage = active_settings(db)
    if not settings.config.get("oss_role_arn") or not storage or storage.provider != "oss":
        raise HTTPException(503, "云电脑工作区挂载尚未就绪，请联系管理员")
    from .storage_billing import check_capacity
    from .shared_resources import materialize_legacy_mount, prefix
    check_capacity(db, access.user_id, 0)
    materialize_legacy_mount(db, access.user_id)
    from .knowledge.originals import ensure_mount_root, prefix as knowledge_prefix
    from .shared_resources import backend as mounted_storage
    store = mounted_storage(db)
    if store is None:
        raise HTTPException(503, "知识库挂载暂不可用，请稍后重试")
    ensure_mount_root(store, access.user_id)
    identifier = uuid4()
    snapshot = {key: value for key, value in settings.config.items() if key != "concurrency"}
    snapshot.update(version=settings.version, resource_mount={"bucket": storage.bucket, "region": storage.region,
        "prefix": prefix(access.user_id), "mode": "shared",
        "knowledge_prefix": knowledge_prefix(access.user_id)})
    reserve = amount(Decimal(str(snapshot["credits_per_minute"])) * snapshot["idle_seconds"] / 60)
    if reserve:
        authorization_hold(db, access.user_id, access.user_id, uuid5(identifier, "hold:0"), reserve)
    row = Computer(id=identifier, user_id=access.user_id, account_id=access.user_id,
        conversation_id=conversation_id, session_hash=access.session_hash, creation_request_id=request_id,
        creation_digest=digest, name=arguments.get("name"), snapshot=snapshot,
        encrypted_api_key=settings.encrypted_api_key, status="starting", held_credits=reserve)
    db.add(row)
    # Durable intent, including the hold, precedes the paid provider request.
    db.commit()
    from sqlalchemy.orm import sessionmaker
    with instance_lease(sessionmaker(bind=db.get_bind()), identifier) as acquired:
        if acquired:
            try:
                if check_active:
                    check_active()
                if on_progress:
                    on_progress({"computer_id": str(row.id), "cloud_computer": read(row), "summary": "正在创建云电脑"})
                provider = adapter(row.snapshot, row.encrypted_api_key)
                provider_id, started_at = provider.create(row.id)
                # Refresh before installing the receipt: a concurrent end intent wins.
                db.refresh(row)
                row.provider_id, row.started_at = provider_id, started_at
                db.commit()
                provider.prepare(provider.connect(provider_id))
                db.refresh(row)
                if not row.stop_requested_at and row.status != "stopping":
                    row.status, row.error = "ready", None
                row.next_reconcile_at = None
                db.commit()
            except Exception as error:
                from .agent.loop import AgentCancelled
                if isinstance(error, AgentCancelled) and row.provider_id is None:
                    db.rollback()
                    db.refresh(row)
                    from .credits import release_hold
                    if row.held_credits:
                        release_hold(db, row.account_id, row.user_id, uuid5(row.id, f"hold:{row.billing_sequence}"), row.held_credits)
                        row.held_credits = Decimal(0)
                    row.status, row.ended_at = "failed", now()
                    row.stop_reason = "user_requested"
                    row.error = None
                    db.commit()
                else:
                    db.rollback()
                    db.refresh(row)
                    if row.status != "stopping":
                        row.status = "uncertain"
                    row.error = "创建结果待核对，请查询此实例，勿重复原操作"
                    db.commit()
    db.refresh(row)
    from .cloud_environment import environment_description
    return {"computer_id": str(row.id), "cloud_computer": read(row, db),
        "environment": environment_description(settings), "uncertain": row.status == "uncertain",
        "summary": "云电脑已创建" if row.status == "ready" else "正在核对创建结果"}


def submit(db, access, conversation_id, request_id, arguments, *, agent_tool_execution_id=None, dispatch_job=True):
    access = owner(db, access, conversation_id)
    from .agent.tool_jobs import validate_owner
    validate_owner(db, agent_tool_execution_id, access, conversation_id)
    file_operation = arguments.get("file_operation")
    if not file_operation or file_operation.get("name") not in {"list_files", "read_file", "inspect_image", "delete_file"}:
        from .storage_billing import check_capacity
        check_capacity(db, access.user_id, 0)
    row = owned_computer(db, access, arguments.get("computer_id"), write=True)
    if (file_operation and file_operation.get("knowledge")
            and not row.snapshot.get("resource_mount", {}).get("knowledge_prefix")):
        raise HTTPException(409, "此云电脑未挂载知识库，请启动新的云电脑")
    # Serialize only retransmissions of this invocation, never unrelated instances.
    invocation_lock(db, request_id)
    payload = {"command": arguments["command"], "inputs": [], "computer_id": str(row.id)}
    if arguments.get("preview"):
        from .cloud_preview import Start
        from .config import get_settings
        if not get_settings().preview_enabled:
            raise HTTPException(503, "成果预览尚未开放，请联系管理员")
        payload["preview"] = Start.model_validate(arguments["preview"]).model_dump()
    if arguments.get("file_operation"):
        payload["file_operation"] = arguments["file_operation"]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(Command).join(Computer).where(Computer.user_id == access.user_id,
        Command.request_id == request_id))
    if existing:
        if existing.digest != digest or existing.agent_tool_execution_id != agent_tool_execution_id:
            raise HTTPException(409, "同一次操作的内容已变化，请先核对执行记录，再重新发起")
        if existing.status == "queued" and dispatch_job and existing.agent_tool_execution_id is None:
            dispatch(existing.id)
        return existing
    from .storage_billing import check_capacity
    check_capacity(db, access.user_id, 1)
    row = db.get(Computer, row.id, with_for_update=True, populate_existing=True)
    if row.ended_at or row.status not in ("ready", "running") or row.stop_requested_at:
        raise HTTPException(409, "指定云电脑暂不可执行，请查询状态或明确创建另一台")
    command = Command(id=uuid4(), session_id=row.id, request_id=request_id, digest=digest, payload=payload,
                      agent_tool_execution_id=agent_tool_execution_id,
                      status="queued", cancel_requested=False, result={})
    db.add(command)
    db.flush()
    if payload.get("preview"):
        from .cloud_preview import attach
        attach(db, row, command, payload["preview"])
    db.commit()
    if dispatch_job and agent_tool_execution_id is None:
        dispatch(command.id)
    return command


def dispatch(identifier):
    from .tasks import run_cloud_command
    try:
        run_cloud_command.delay(str(identifier))
    except Exception:
        logger.warning("Cloud command dispatch deferred")


def mount_resources(db, access, explicit):
    """Legacy helper: shared mounts do not stage resource bodies into the sandbox."""
    raise HTTPException(410, "资源库已直接挂载到云电脑，无需准备单独输入文件")


def mount_storage(db, row):
    from .storage_backend import current_storage
    storage = current_storage(db)
    mount = row.snapshot["resource_mount"]
    if mount.get("mode") != "shared":
        raise HTTPException(503, "请先结束旧云电脑，再重试")
    if getattr(storage, "bucket", None) != mount["bucket"] or getattr(storage, "region", None) != mount["region"]:
        raise HTTPException(409, "文件存储已变化，请先结束当前云电脑")
    return storage


def stage_mount(db, row, access, explicit, provider=None, sandbox=None):
    """Validate the shared mount; resource files are never staged into a sandbox."""
    mount_storage(db, row)


def cleanup_mount(db, row):
    return


def synchronize(db, row, provider, sandbox):
    mount_storage(db, row)
    return []


def recover_mount(db, row):
    mount_storage(db, row)
    return []


def run(identifier, factory=SessionLocal, *, agent_tool_execution_id=None, check_active=None, on_progress=None):
    identifier = UUID(str(identifier))
    with factory() as lease_db, lease_db.get_bind().connect() as connection:
        lock = int.from_bytes(identifier.bytes[-8:], "big", signed=True)
        pg = connection.dialect.name == "postgresql"
        if pg and not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}):
            return
        try:
            _run(identifier, factory, agent_tool_execution_id=agent_tool_execution_id,
                check_active=check_active, on_progress=on_progress)
        finally:
            if pg:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})


def _run(identifier, factory=SessionLocal, *, agent_tool_execution_id=None, check_active=None, on_progress=None):
    """Claim once; never replay a command whose upstream submission was uncertain."""
    import time
    with factory() as db:
        command = db.get(Command, UUID(str(identifier)), with_for_update=True)
        if (not command or command.status != "queued"
                or command.agent_tool_execution_id != agent_tool_execution_id):
            return
        row = db.get(Computer, command.session_id, with_for_update=True)
        def cancelled():
            db.refresh(command)
            if command.agent_tool_execution_id:
                from .agent.tool_jobs import owner_active
                if not owner_active(db, command.agent_tool_execution_id):
                    command.cancel_requested = True
                    db.commit()
            if check_active:
                try:
                    check_active()
                except Exception:
                    command.cancel_requested = True
                    db.commit()
            return command.cancel_requested

        def progress():
            if on_progress:
                try:
                    on_progress({"cloud_computer": read(row), "command_id": str(command.id),
                        "summary": "正在准备云电脑" if command.status == "preparing" else "正在执行"})
                except Exception:
                    logger.warning("Cloud progress delivery deferred: %s", command.id)

        cancelled()
        if row.ended_at or row.status in ("stopping", "uncertain") or command.cancel_requested:
            command.status, command.finished_at = "cancelled", now()
            command.result = {"status": "cancelled", "error": "命令已停止", "error_code": "cloud_cancelled"}
            db.commit()
            return
        command.status, command.started_at, command.heartbeat_at = "preparing", now(), now()
        row.last_activity_at = now()
        db.commit()
        progress()
        submitted = False
        try:
            provider = adapter(row.snapshot, row.encrypted_api_key)
            access = session_access(db, row)
            if row.snapshot.get("resource_mount", {}).get("mode") != "shared":
                raise HTTPException(503, "云电脑未挂载工作区资源目录")
            if not row.provider_id:
                raise HTTPException(409, "指定云电脑尚未就绪，请查询状态")
            sandbox = provider.connect(row.provider_id)
            provider.prepare(sandbox)
            stage_mount(db, row, access, command.payload["inputs"], provider, sandbox)
            db.refresh(command)
            db.refresh(row, ["status", "billing_stopped_at"])
            if cancelled() or row.status == "stopping":
                command.status, command.finished_at = "cancelled", now()
                command.result = {"status": "cancelled", "error": "命令已停止"}
                db.commit()
                return
            if command.payload.get("preview"):
                from .models import CloudPreview
                preview = db.scalar(select(CloudPreview).where(CloudPreview.command_id == command.id))
                if preview.ended_at or preview.status == "stopping":
                    command.status, command.finished_at = "cancelled", now()
                    command.result = {"status": "cancelled", "error": "预览已停止"}
                    db.commit()
                    return
            if command.payload.get("file_operation", {}).get("reference"):
                from .workspace_files import resolve_input
                resolve_input(db, access, command.payload["file_operation"]["reference"])
            row.status = "running"
            command.status = "running"
            db.commit()
            progress()
            submitted = True
            if command.payload.get("preview"):
                command.process_id = provider.start_preview(sandbox, command.id, command.payload["preview"])
            elif command.payload.get("file_operation"):
                command.process_id = provider.start_file(sandbox, command.id, command.payload["file_operation"])
            else:
                command.process_id = provider.start(sandbox, command.id, command.payload["command"])
            db.commit()
            while True:
                db.refresh(command)
                db.refresh(row)
                session_access(db, row)
                if cancelled() or row.status == "stopping" or now() >= aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]):
                    if command.payload.get("preview"):
                        from .cloud_preview import stop
                        preview_result = stop(db, access, preview, row)
                        if preview_result["status"] != "stopped":
                            raise RuntimeError("preview cancellation unconfirmed")
                    else:
                        provider.cancel(sandbox, command.id, command.process_id)
                    command.status = "cancelled"
                    command.result = {"status": "cancelled", "error": "云电脑命令已停止"}
                    if command.payload.get("preview"):
                        command.result = {**command.result, "preview": preview_result}
                    break
                result = provider.poll(sandbox, command.id)
                if result is not None:
                    command.result = {**result, "status": "completed" if result["exit_code"] == 0 else "failed"}
                    if command.payload.get("file_operation"):
                        file_result = provider.file_result(sandbox, command.id)
                        command.result = {**command.result, "file_result": file_result}
                        if file_result.get("error"):
                            command.result.update(status="failed", error=file_result["error"],
                                error_code="file_conflict" if file_result["status_code"] == 409 else "file_arguments")
                    if result["exit_code"] != 0:
                        command.result = {**command.result, "error": "命令未能完成，请根据执行结果调整后再试", "error_code": "script_failed"}
                    command.status = "completed" if command.result.get("status") == "completed" else command.result.get("status", "failed")
                    if command.payload.get("preview"):
                        from .cloud_preview import finish_command
                        finish_command(db, command, row, provider, sandbox)
                    db.commit()
                    break
                command.heartbeat_at = now()
                db.commit()
                time.sleep(1)
            command.finished_at = now()
            db.commit()
        except Exception:
            db.rollback()
            db.refresh(command)
            db.refresh(row)
            command.status = "uncertain" if submitted else "failed"
            command.result = {"status": command.status, "error": "执行结果待核对，请查看资源库，勿重复执行" if submitted else "云电脑未能准备文件，请检查资源与额度后重试",
                                  "uncertain": submitted}
            if submitted and command.payload.get("preview"):
                from .models import CloudPreview
                preview = db.scalar(select(CloudPreview).where(CloudPreview.command_id == command.id))
                if preview:
                    preview.status = "uncertain"
                    preview.error = "正在核对启动状态，请勿重复启动"
                    from .cloud_preview import read as read_preview
                    command.result = {**command.result, "preview": read_preview(preview)}
            elif submitted:
                # An uncertain command must not destroy other independent work.
                command.heartbeat_at = now()
            elif row.provider_id:
                command.heartbeat_at = now()
            else:
                row.status = "uncertain"
            logger.warning("Cloud command failed: %s", command.id, exc_info=False)
        finally:
            command.finished_at = now()
            db.refresh(row, with_for_update=True)
            if not row.ended_at and row.status not in ("stopping", "uncertain"):
                busy = db.scalar(select(Command.id).where(Command.session_id == row.id, Command.status.in_(BUSY)).limit(1))
                row.status, row.last_activity_at = ("running" if busy else "ready"), now()
            db.commit()


def execute(db, access, conversation_id, request_id, arguments, check_active=None, *,
            agent_tool_execution_id=None, on_progress=None):
    """Execute in the Agent worker, without queueing work behind that same worker."""
    if arguments.get("inputs"):
        raise HTTPException(422, "云电脑工作区已直接挂载，请移除旧式文件输入参数")
    from sqlalchemy.orm import sessionmaker
    command = submit(db, access, conversation_id, request_id, arguments,
        agent_tool_execution_id=agent_tool_execution_id, dispatch_job=False)
    # End the caller's read transaction before the runner takes ownership.
    identifier = command.id
    db.commit()
    run(identifier, sessionmaker(bind=db.get_bind()), agent_tool_execution_id=agent_tool_execution_id,
        check_active=check_active, on_progress=on_progress)
    db.refresh(command)
    row = db.get(Computer, command.session_id, populate_existing=True)
    if command.status in BUSY:
        return {"command_id": str(command.id), "uncertain": True, "error": "命令结果尚未确认，请勿重复执行"}
    return {**command.result, "computer_id": str(row.id), "cloud_computer": read(row), "command_id": str(command.id)}


def execute_image(db, access, conversation_id, request_id, path, check_active, *, computer_id,
                  agent_tool_execution_id=None, on_progress=None):
    """Inspect a mounted image durably, then read its bytes outside command results."""
    from .conversations.attachments import MAX_IMAGE_BYTES
    from .image_reading import local_path, workspace_image
    import hashlib

    try:
        sandbox_path = local_path(path)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    result = execute(db, access, conversation_id, request_id,
        {"command": "inspect_image", "computer_id": computer_id,
         "file_operation": {"name": "inspect_image", "path": sandbox_path}}, check_active,
        agent_tool_execution_id=agent_tool_execution_id, on_progress=on_progress)
    command = db.get(Command, UUID(result["command_id"]))
    try:
        row = db.get(Computer, command.session_id, populate_existing=True)
        file_result = result.get("file_result", {})
        if file_result.get("error"):
            raise HTTPException(file_result.get("status_code", 422), file_result["error"])
        if result.get("status") != "completed":
            raise HTTPException(503, "图片未能读取，请稍后重试")
        metadata = file_result.get("value") or {}
        if (not isinstance(metadata.get("size_bytes"), int)
                or metadata["size_bytes"] <= 0 or metadata["size_bytes"] > MAX_IMAGE_BYTES
                or not isinstance(metadata.get("sha256"), str)):
            raise HTTPException(503, "图片读取结果无效，请重试")
        provider = adapter(row.snapshot, row.encrypted_api_key)
        sandbox = provider.connect(row.provider_id)
        def check_read_access():
            if check_active:
                check_active()
            checked = access.revalidate(db)
            owned_computer(db, checked, row.id)
            db.refresh(row)
            if row.status not in ("ready", "running") or (row.started_at and
                    now() >= aware(row.started_at) + timedelta(seconds=row.snapshot["max_seconds"])):
                raise HTTPException(409, "云电脑已结束，请重新读取图片")
        from .upstream_retries import retry_scope
        import time
        remaining = ((aware(row.started_at) + timedelta(seconds=row.snapshot["max_seconds"]) - now()).total_seconds()
            if row.started_at else 30)
        with retry_scope(check_read_access, deadline=time.monotonic() + max(0, remaining)):
            data = provider.read_file(sandbox, sandbox_path, max_bytes=MAX_IMAGE_BYTES,
                check_active=check_read_access)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != metadata["size_bytes"] or digest != metadata["sha256"]:
            raise HTTPException(409, "图片在读取过程中发生变化，请重新读取")
        from .image_reading import image_type
        content_type, width, height = image_type(data)
        return workspace_image(sandbox_path, content_type, width, height, digest, data), result
    except BaseException:
        if command.status in BUSY:
            command.cancel_requested = True
            db.commit()
        raise


_scheduled = set()
_schedule_guard = threading.Lock()


def maintenance(factory=SessionLocal, *, wait=True):
    """Reconcile lifetime independently of browser/Agent, without replaying commands."""
    with factory() as db:
        query = select(Computer.id).where(Computer.ended_at.is_(None))
        if not wait:
            query = query.where((Computer.next_reconcile_at.is_(None)) | (Computer.next_reconcile_at <= now()))
        identifiers = list(db.scalars(query))
    for identifier in identifiers:
        if wait:
            maintain_one(identifier, factory)
            continue
        with _schedule_guard:
            if identifier in _scheduled:
                continue
            _scheduled.add(identifier)
        def reconcile_one(identifier=identifier):
            try:
                maintain_one(identifier, factory)
            finally:
                with _schedule_guard:
                    _scheduled.discard(identifier)
        threading.Thread(target=reconcile_one, daemon=True, name=f"cloud-recovery-{identifier}").start()


def maintain_one(identifier, factory=SessionLocal):
    with instance_lease(factory, identifier) as acquired:
        if not acquired:
            return
        try:
            with factory() as db:
                _maintain(db, identifier)
                row = db.get(Computer, identifier, populate_existing=True)
                if row:
                    row.recovery_attempts = 0
                    row.next_reconcile_at = now() + timedelta(seconds=15)
                    db.commit()
        except Exception:
            logger.warning("Cloud computer reconciliation deferred: %s", identifier, exc_info=False)
            with factory() as db:
                row = db.get(Computer, identifier, with_for_update=True)
                if row and not row.ended_at:
                    row.recovery_attempts = (row.recovery_attempts or 0) + 1
                    row.next_reconcile_at = now() + timedelta(seconds=min(300, 15 * 2 ** min(5, row.recovery_attempts)))
                    db.commit()


def _maintain(db, identifier):
    row = db.get(Computer, identifier, populate_existing=True)
    if not row or row.ended_at:
        return
    provider = adapter(row.snapshot, row.encrypted_api_key)
    commands = list(db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))))
    # Re-dispatch only unclaimed jobs. Claimed execution is never submitted twice.
    for command in commands:
        if command.status == "queued":
            if command.agent_tool_execution_id is None:
                dispatch(command.id)
            else:
                from .agent.tool_jobs import owner_active
                if not owner_active(db, command.agent_tool_execution_id):
                    command.status, command.finished_at, command.cancel_requested = "cancelled", now(), True
                    command.result = {"status": "cancelled", "error": "执行已中断，命令尚未提交"}
                    db.commit()
    if row.ended_at:
        return
    if not row.provider_id:
        if (now()-aware(row.created_at)).total_seconds() < 90:
            return
        found = provider.find(row.id)
        if len(found) > 1:
            # Re-query on the next pass; never mark destruction confirmed from one
            # lost/failed response, and never create another copy of this intent.
            for provider_id, _ in found:
                provider.kill(provider_id)
            row.status, row.billing_stopped_at = "stopping", now()
            row.stop_requested_at = row.stop_requested_at or now()
            set_stop_reason(db, row, "execution_interrupted")
            db.commit()
            return
        if not found:
            # Provider create uses a bounded request and maximum lifetime. Keep
            # the slot during this window to avoid assuming a delayed create failed.
            if now() < aware(row.created_at)+timedelta(seconds=row.snapshot["max_seconds"]+120):
                return
            set_stop_reason(db, row, "startup_failed")
            reconcile(db, row, now(), final=True)
            row.status, row.ended_at = "failed", now()
            db.commit()
            return
        db.refresh(row)
        row.provider_id, row.started_at = found[0]
        if row.creation_request_id and not row.stop_requested_at and row.status != "stopping":
            provider.prepare(provider.connect(row.provider_id))
            row.status, row.error = "ready", None
        else:
            row.status = "stopping"
            row.billing_stopped_at = row.billing_stopped_at or row.started_at
            set_stop_reason(db, row, "execution_interrupted")
        db.commit()
    if not row.provider_id:
        return
    alive = provider.exists(row.provider_id)
    if alive and row.status in ("starting", "uncertain") and row.creation_request_id and not row.stop_requested_at:
        provider.prepare(provider.connect(row.provider_id))
        db.refresh(row)
        if not row.stop_requested_at:
            row.status, row.error = "ready", None
        db.commit()
    from .cloud_preview import maintain as maintain_preview, idle_anchor
    try:
        maintain_preview(db, row, provider, alive)
    except Exception:
        # A failed service health probe must not suppress billing or VM expiry.
        logger.warning("Cloud preview health check deferred: %s", identifier, exc_info=False)
    if not alive:
        end = min(now(), aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]))
        # If disappearance was unexpected, charge no unconfirmed interval.
        if row.status != "stopping" and end < aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]):
            end = aware(row.started_at)+timedelta(seconds=row.billed_seconds)
        if row.status != "stopping":
            set_stop_reason(db, row, "max_lifetime" if end >= aware(row.started_at)
                + timedelta(seconds=row.snapshot["max_seconds"]) else "provider_ended")
        reconcile(db, row, end, final=True)
        row.status, row.ended_at = "ended", now()
        for command in commands:
            command.status, command.finished_at = "uncertain", now()
            command.result = {"error": "云电脑已结束，请检查资源库中的输出，勿重复执行", "uncertain": True}
        db.commit()
        return
    unresolved = list(db.scalars(select(Command).where(Command.session_id == row.id, Command.status == "uncertain")))
    recoverable = [command for command in commands + unresolved if command.status != "queued"
        and command.heartbeat_at and (now()-aware(command.heartbeat_at)).total_seconds() > 45]
    for command in recoverable:
        if db.get_bind().dialect.name == "postgresql":
            with db.get_bind().connect() as connection:
                lock = int.from_bytes(command.id.bytes[-8:], "big", signed=True)
                if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}):
                    continue
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
        sandbox = provider.connect(row.provider_id)
        result = provider.poll(sandbox, command.id) if command.status != "preparing" else None
        if result is not None:
            command.result = {**result, "status": "completed" if result.get("exit_code") == 0 else "failed"}
            if command.payload.get("file_operation"):
                file_result = provider.file_result(sandbox, command.id)
                command.result = {**command.result, "file_result": file_result}
                if file_result.get("error"):
                    command.result.update(status="failed", error=file_result["error"])
            command.status = command.result["status"]
            if command.payload.get("preview"):
                from .cloud_preview import finish_command
                finish_command(db, command, row, provider, sandbox)
            command.finished_at = now()
            db.refresh(row, with_for_update=True)
            if row.status != "stopping" and not row.ended_at:
                row.last_activity_at = now()
            db.commit()
        else:
            # Never replay lost submission. Cancel only this command, leaving
            # other commands/instances alone; a missing receipt stays uncertain.
            provider.cancel(sandbox, command.id, command.process_id)
            command.status = "uncertain"
            command.result = {"status": "uncertain", "uncertain": True, "error": "命令结果待核对，请检查已有输出，勿重复执行"}
            command.heartbeat_at = now()
            db.commit()
    commands = list(db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))))
    db.refresh(row, with_for_update=True)
    if row.stop_requested_at:
        row.status = "stopping"
    elif row.status in ("ready", "running"):
        row.status = "running" if commands else "ready"
    try:
        session_access(db, row)
    except HTTPException:
        row.status, row.billing_stopped_at = "stopping", row.billing_stopped_at or now()
        set_stop_reason(db, row, "access_revoked")
    expired = now() >= aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"])
    anchor = idle_anchor(db, row)
    idle = not commands and now() >= anchor+timedelta(seconds=row.snapshot["idle_seconds"])
    if expired or idle:
        row.status = "stopping"
        cutoff = aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]) if expired else anchor+timedelta(seconds=row.snapshot["idle_seconds"])
        row.billing_stopped_at = row.billing_stopped_at or cutoff
        set_stop_reason(db, row, "max_lifetime" if expired else "idle_timeout")
    if row.status != "stopping" and charge_at(row, now())[0] >= row.billed_seconds + 60:
        if reconcile(db, row, now()) is not ReconcileResult.CONTINUE:
            row.status = "stopping"
    db.commit()
    if row.status != "stopping":
        return
    sandbox = None
    try:
        sandbox = provider.connect(row.provider_id)
    except Exception:
        logger.warning("Cloud final connection failed: %s", identifier, exc_info=False)
    for command in commands:
        command.cancel_requested = True
        try:
            if sandbox is not None:
                provider.cancel(sandbox, command.id, command.process_id)
        except Exception:
            # Instance destruction is still required when its process is absent
            # or the command channel has stopped responding.
            logger.warning("Cloud process cancellation unconfirmed: %s", command.id, exc_info=False)
        command.status, command.finished_at = "cancelled", now()
        command.result = {"status": "cancelled", "error": "云电脑已停止"}
    db.commit()
    if not provider.kill(row.provider_id):
        db.commit()
        logger.error("Cloud computer destruction not confirmed: %s", identifier)
        raise TimeoutError("供应商尚未确认实例已销毁")
    maintain_preview(db, row, provider, False)
    db.refresh(row, with_for_update=True)
    reconcile(db, row, now(), final=True)
    row.status, row.ended_at = "ended", now()
    db.commit()
