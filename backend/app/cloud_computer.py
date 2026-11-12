"""Durable cloud execution, resource synchronization and metered session lifetime."""
import hashlib
import json
import logging
import math
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import func, select, text

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


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    region: str = Field(default="", max_length=60, pattern=r"^([a-z0-9]+(-[a-z0-9]+)*)?$")
    template: str = Field(default="", max_length=200, pattern=r"^[a-zA-Z0-9._/-]*$")
    specification: str = Field(default="", max_length=120)
    credits_per_minute: CreditRate | None = Decimal("1")
    idle_seconds: int = Field(default=600, ge=60, le=1800)
    max_seconds: int = Field(default=3600, ge=300, le=3600)
    concurrency: int = Field(default=5, ge=1, le=100)
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
    return owner(db, Access(row.user_id, row.workspace_id, "editor", row.session_hash), row.conversation_id)


def charge_at(row, end):
    if not row.started_at:
        return 0, Decimal(0)
    end = min(aware(end), aware(row.started_at) + timedelta(seconds=row.snapshot["max_seconds"]))
    if row.billing_stopped_at:
        end = min(end, aware(row.billing_stopped_at))
    seconds = max(0, math.floor((end - aware(row.started_at)).total_seconds()))
    rate = Decimal(str(row.snapshot["credits_per_minute"]))
    return seconds, amount(rate * seconds / 60)


def reconcile(db, row, end, final=False):
    """Round cumulative usage once; intervals book only the cumulative delta."""
    seconds, total = charge_at(row, end)
    limit = row.charged_credits + row.held_credits
    if total > limit:
        rate = Decimal(str(row.snapshot["credits_per_minute"]))
        seconds = int((limit * 60 / rate).to_integral_value(rounding=ROUND_FLOOR)) if rate else seconds
        row.billing_stopped_at = aware(row.started_at) + timedelta(seconds=seconds)
        total = amount(rate * seconds / 60)
    delta = max(Decimal(0), total - row.charged_credits)
    if row.held_credits:
        release_hold(db, row.workspace_id, row.user_id, uuid5(row.id, f"hold:{row.billing_sequence}"), row.held_credits)
    row.held_credits = Decimal(0)
    if delta:
        _entry(db, ensure_account(db, row.workspace_id), actor_user_id=row.user_id, amount=-delta,
            kind="usage_settlement", idempotency_key=f"cloud:{row.id}:charge:{row.billing_sequence}",
            metadata={"capability": "cloud_computer", "computer_id": str(row.id), "seconds": seconds - row.billed_seconds})
        db.add(UsageEvent(workspace_id=row.workspace_id, actor_user_id=row.user_id, request_id=row.id,
            capability="cloud_computer", units=max(0, seconds-row.billed_seconds), credits=delta,
            policy_version=row.snapshot["version"], policy_snapshot={k: v for k, v in row.snapshot.items() if k != "blocked_hosts"},
            idempotency_key=f"cloud:{row.id}:usage:{row.billing_sequence}", metadata_json={"computer_id": str(row.id)}))
    row.charged_credits, row.billed_seconds = total, seconds
    row.billing_sequence += 1
    if not final and not row.billing_stopped_at:
        reserve = amount(Decimal(str(row.snapshot["credits_per_minute"])) * row.snapshot["idle_seconds"] / 60)
        account = ensure_account(db, row.workspace_id)
        if account.balance < reserve:
            row.billing_stopped_at = end
            return False
        if reserve:
            authorization_hold(db, row.workspace_id, row.user_id, uuid5(row.id, f"hold:{row.billing_sequence}"), reserve)
        row.held_credits = reserve
    return not row.billing_stopped_at


def read(row, db=None):
    labels = {"starting": "正在启动云电脑", "ready": "云电脑运行中", "running": "正在执行", "stopping": "正在结束云电脑",
              "uncertain": "正在核对云电脑状态", "ended": "云电脑已结束", "failed": "云电脑未能启动"}
    seconds, estimated = charge_at(row, now())
    from .cloud_preview import idle_anchor
    anchor = idle_anchor(db, row) if db is not None else aware(row.last_activity_at)
    return {"id": str(row.id), "status": row.status, "summary": row.error or labels[row.status],
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


@router.get("/conversations/{conversation_id}/cloud-computer")
def current(conversation_id: UUID, db=Depends(get_db), access=Depends(get_access)):
    owner(db, access, conversation_id)
    row = db.scalar(select(Computer).where(Computer.conversation_id == conversation_id,
        Computer.user_id == access.user_id, Computer.workspace_id == access.workspace_id).order_by(Computer.created_at.desc()))
    return {"computer": read(row, db) if row else None}


@router.post("/conversations/{conversation_id}/cloud-computer/end")
def end(conversation_id: UUID, db=Depends(get_db), access=Depends(get_access)):
    access = owner(db, access, conversation_id)
    access.editor()
    row = db.scalar(select(Computer).where(Computer.conversation_id == conversation_id,
        Computer.user_id == access.user_id, Computer.workspace_id == access.workspace_id, Computer.ended_at.is_(None)).with_for_update())
    if row:
        row.status = "stopping"
        row.billing_stopped_at = row.billing_stopped_at or now()
        for command in db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))):
            command.cancel_requested = True
        db.commit()
    return {"computer": read(row) if row else None}


def command_read(command):
    return {"id": str(command.id), "status": command.status, "cancel_requested": command.cancel_requested,
            "result": command.result, "terminal": command.status not in BUSY}


@router.get("/conversations/{conversation_id}/cloud-computer/commands")
def command_list(conversation_id: UUID, db=Depends(get_db), access=Depends(get_access)):
    owner(db, access, conversation_id)
    rows = db.scalars(select(Command).join(Computer).where(Computer.conversation_id == conversation_id,
        Computer.user_id == access.user_id, Computer.workspace_id == access.workspace_id)
        .order_by(Command.created_at.desc()).limit(20)).all()
    return {"items": [command_read(command) for command in rows]}


def own_command(db, access, identifier):
    command = db.get(Command, identifier, with_for_update=True)
    row = db.get(Computer, command.session_id) if command else None
    if not row or row.user_id != access.user_id or row.workspace_id != access.workspace_id:
        raise HTTPException(404, "命令不存在")
    owner(db, access, row.conversation_id).editor()
    return command, row


@router.post("/cloud-computer/commands/{identifier}/cancel")
def cancel_command(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    command, _ = own_command(db, access, identifier)
    command.cancel_requested = True
    db.commit()
    return command_read(command)


def submit(db, access, conversation_id, request_id, arguments, skill_packages=None):
    access = owner(db, access, conversation_id)
    file_operation = arguments.get("file_operation")
    if not file_operation or file_operation.get("name") not in {"list_files", "read_file", "inspect_image"}:
        access.editor()
        if not file_operation or file_operation.get("name") != "delete_file":
            from .storage_billing import check_capacity
            check_capacity(db, access.workspace_id, 0)
    serialize_admission(db)
    payload = {"command": arguments["command"], "inputs": [], "skills": skill_packages or {}}
    if arguments.get("preview"):
        from .cloud_preview import Start
        from .config import get_settings
        if not get_settings().preview_enabled:
            raise HTTPException(503, "成果预览尚未开放，请联系管理员")
        payload["preview"] = Start.model_validate(arguments["preview"]).model_dump()
    if arguments.get("file_operation"):
        payload["file_operation"] = arguments["file_operation"]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(Command).join(Computer).where(Computer.workspace_id == access.workspace_id,
        Computer.user_id == access.user_id, Computer.conversation_id == conversation_id,
        Command.request_id == request_id, Command.digest == digest))
    if existing:
        if existing.status == "queued":
            dispatch(existing.id)
        return existing
    from .storage_billing import check_capacity
    check_capacity(db, access.workspace_id, 1)
    settings = db.get(Settings, 1, populate_existing=True)
    if not settings or not settings.enabled:
        raise HTTPException(503, "云电脑尚未开放，请联系管理员")
    from .storage_backend import active_settings
    active_storage = active_settings(db)
    if not settings.config.get("oss_role_arn") or not active_storage or active_storage.provider != "oss":
        raise HTTPException(503, "云电脑工作区挂载尚未就绪，请联系管理员")
    row = db.scalar(select(Computer).where(Computer.user_id == access.user_id, Computer.ended_at.is_(None)).with_for_update())
    if row and (row.workspace_id != access.workspace_id or row.conversation_id != conversation_id):
        raise HTTPException(409, "你在其他会话有运行中的云电脑，请先结束后重试")
    if row and row.status in ("stopping", "uncertain"):
        raise HTTPException(409, "云电脑正在结束或核对状态，请稍后重试")
    if row and row.snapshot.get("resource_mount", {}).get("mode") != "shared":
        raise HTTPException(409, "请先结束当前云电脑，再重试文件操作")
    if row is None:
        count = db.scalar(select(func.count()).select_from(Computer).where(Computer.ended_at.is_(None)))
        if count >= settings.config["concurrency"]:
            raise HTTPException(409, "云电脑繁忙，请稍后重试")
        identifier = uuid4()
        snapshot = {**settings.config, "version": settings.version}
        snapshot["resource_mount"] = {"bucket": active_storage.bucket, "region": active_storage.region,
            "prefix": f"workspaces/{access.workspace_id}/resource-directory/", "mode": "shared"}
        reserve = amount(Decimal(str(settings.config["credits_per_minute"])) * settings.config["idle_seconds"] / 60)
        if reserve:
            authorization_hold(db, access.workspace_id, access.user_id, uuid5(identifier, "hold:0"), reserve)
        row = Computer(id=identifier, workspace_id=access.workspace_id, user_id=access.user_id,
            conversation_id=conversation_id, session_hash=access.session_hash, status="starting",
            snapshot=snapshot, encrypted_api_key=settings.encrypted_api_key,
            manifest={}, held_credits=reserve, charged_credits=0, billed_seconds=0, billing_sequence=0)
        db.add(row)
        db.flush()
    if db.scalar(select(Command.id).where(Command.session_id == row.id, Command.status.in_(BUSY))):
        raise HTTPException(409, "云电脑已有命令正在执行，请等待完成")
    command = Command(id=uuid4(), session_id=row.id, request_id=request_id, digest=digest, payload=payload,
                      status="queued", cancel_requested=False, result={})
    db.add(command)
    db.flush()
    if payload.get("preview"):
        from .cloud_preview import attach
        attach(db, row, command, payload["preview"])
    db.commit()
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


def run(identifier, factory=SessionLocal):
    identifier = UUID(str(identifier))
    with factory() as lease_db, lease_db.get_bind().connect() as connection:
        lock = int.from_bytes(identifier.bytes[-8:], "big", signed=True)
        pg = connection.dialect.name == "postgresql"
        if pg and not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}):
            return
        try:
            _run(identifier, factory)
        finally:
            if pg:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})


def _run(identifier, factory=SessionLocal):
    """Claim once; never replay a command whose upstream submission was uncertain."""
    import base64
    import time
    with factory() as db:
        command = db.get(Command, UUID(str(identifier)), with_for_update=True)
        if not command or command.status != "queued":
            return
        row = db.get(Computer, command.session_id, with_for_update=True)
        if row.ended_at or row.status in ("stopping", "uncertain") or command.cancel_requested:
            command.status, command.finished_at = "cancelled", now()
            db.commit()
            return
        command.status, command.started_at, command.heartbeat_at = "preparing", now(), now()
        row.last_activity_at = now()
        db.commit()
        submitted = False
        try:
            provider = adapter(row.snapshot, row.encrypted_api_key)
            access = session_access(db, row)
            access.editor()
            if row.snapshot.get("resource_mount", {}).get("mode") != "shared":
                raise HTTPException(503, "云电脑未挂载工作区资源目录")
            if not row.provider_id:
                stage_mount(db, row, access, command.payload["inputs"])
                try:
                    row.provider_id, row.started_at = provider.create(row.id)
                except Exception:
                    row.status, row.error = "uncertain", "云电脑启动结果待核对，请勿重复启动"
                    command.status, command.finished_at = "uncertain", now()
                    db.commit()
                    return
                row.status = "ready"
                db.commit()
            sandbox = provider.connect(row.provider_id)
            provider.prepare(sandbox)
            stage_mount(db, row, access, command.payload["inputs"], provider, sandbox)
            clear_skills = getattr(provider, "clear_skills", None)
            if clear_skills:
                # A session reuses the cloud computer; never leave a package from
                # an earlier command visible to a later, unselected command.
                clear_skills(sandbox)
            for skill_id, package in command.payload["skills"].items():
                for path, encoded in package.items():
                    provider.write_skill(sandbox, f"{skill_id}/{path}", base64.b64decode(encoded))
            db.refresh(command)
            db.refresh(row, ["status", "billing_stopped_at"])
            if command.cancel_requested or row.status == "stopping":
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
                session_access(db, row).editor()
                if command.cancel_requested or row.status == "stopping" or now() >= aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]):
                    provider.cancel(sandbox, command.id, command.process_id)
                    command.status = "cancelled"
                    command.result = {"status": "cancelled", "error": "云电脑命令已停止"}
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
            elif submitted:
                row.status = "stopping"
                row.billing_stopped_at = now()
            elif row.provider_id:
                row.status = "stopping"
                row.billing_stopped_at = row.started_at
            else:
                reconcile(db, row, now(), final=True)
                row.status, row.ended_at = "failed", now()
                db.commit()
            logger.warning("Cloud command failed: %s", command.id, exc_info=False)
        finally:
            command.finished_at = now()
            db.refresh(row, ["status", "ended_at"])
            if not row.ended_at and row.status not in ("stopping", "uncertain"):
                row.status, row.last_activity_at = "ready", now()
            # Do not retain potentially large Skill payloads after dispatch.
            if command.status not in ("queued", "preparing"):
                command.payload = {**command.payload, "skills": {}}
            db.commit()


def execute(db, access, conversation_id, request_id, arguments, skill_packages, check_active):
    """Agent waits for the durable command; UI and lifecycle do not depend on it."""
    if arguments.get("inputs"):
        raise HTTPException(422, "云电脑工作区已直接挂载，请移除旧式文件输入参数")
    import base64
    import time
    packages = {str(identifier): {path: base64.b64encode(data).decode() for path, data in files.items()}
                for identifier, files in (skill_packages or {}).items()}
    command = submit(db, access, conversation_id, request_id, arguments, packages)
    while command.status in BUSY:
        if check_active:
            try:
                check_active()
            except BaseException:
                if not command.payload.get("preview"):
                    command.cancel_requested = True
                db.commit()
                raise
        time.sleep(.5)
        db.refresh(command)
    row = db.get(Computer, command.session_id, populate_existing=True)
    return {**command.result, "cloud_computer": read(row), "command_id": str(command.id)}


def execute_image(db, access, conversation_id, request_id, path, check_active):
    """Inspect a mounted image durably, then read its bytes outside command results."""
    from .chat_attachments import MAX_IMAGE_BYTES
    from .image_reading import local_path, workspace_image
    import hashlib

    try:
        sandbox_path = local_path(path)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    command = submit(db, access, conversation_id, request_id,
        {"command": "inspect_image",
         "file_operation": {"name": "inspect_image", "path": sandbox_path}}, None)
    try:
        while command.status in BUSY:
            if check_active:
                check_active()
            import time
            time.sleep(.5)
            db.refresh(command)
        row = db.get(Computer, command.session_id, populate_existing=True)
        result = {**command.result, "cloud_computer": read(row), "command_id": str(command.id)}
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
            owner(db, checked, conversation_id)
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


def maintenance(factory=SessionLocal):
    """Reconcile lifetime independently of browser/Agent, without replaying commands."""
    with factory() as db:
        identifiers = list(db.scalars(select(Computer.id).where(Computer.ended_at.is_(None))))
    for identifier in identifiers:
        try:
            maintain_one(identifier, factory)
        except Exception:
            logger.warning("Cloud computer reconciliation deferred: %s", identifier, exc_info=False)


def maintain_one(identifier, factory=SessionLocal):
    with factory() as db, db.get_bind().connect() as connection:
        # Session advisory lock spans provider IO, preventing concurrent destroy/
        # recovery on multiple API workers. It is released even after an exception.
        lock = int.from_bytes(identifier.bytes[:8], "big", signed=True)
        pg = connection.dialect.name == "postgresql"
        if pg and not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}):
            return
        try:
            _maintain(db, identifier)
        finally:
            if pg:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})


def _maintain(db, identifier):
    row = db.get(Computer, identifier, populate_existing=True)
    if not row or row.ended_at:
        return
    provider = adapter(row.snapshot, row.encrypted_api_key)
    commands = list(db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))))
    # Re-dispatch only unclaimed jobs. Claimed execution is never submitted twice.
    for command in commands:
        if command.status == "queued":
            dispatch(command.id)
    if row.status in ("starting", "uncertain") and not row.provider_id:
        if (now()-aware(row.created_at)).total_seconds() < 90:
            return
        found = provider.find(row.id)
        if len(found) > 1:
            for provider_id, _ in found:
                provider.kill(provider_id)
            row.status, row.billing_stopped_at = "stopping", now()
            db.commit()
            return
        if not found:
            # Provider create uses a bounded request and maximum lifetime. Keep
            # the slot during this window to avoid assuming a delayed create failed.
            if now() < aware(row.created_at)+timedelta(seconds=row.snapshot["max_seconds"]+120):
                return
            reconcile(db, row, now(), final=True)
            row.status, row.ended_at = "failed", now()
            db.commit()
            return
        row.provider_id, row.started_at = found[0]
        row.status, row.billing_stopped_at = "stopping", row.started_at
        db.commit()
    if not row.provider_id:
        return
    alive = provider.exists(row.provider_id)
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
        reconcile(db, row, end, final=True)
        row.status, row.ended_at = "ended", now()
        for command in commands:
            command.status, command.finished_at = "uncertain", now()
            command.result = {"error": "云电脑已结束，请检查资源库中的输出，勿重复执行", "uncertain": True}
        db.commit()
        return
    recoverable = [command for command in commands if command.status != "queued"
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
            row.status, row.last_activity_at = "ready", now()
            db.commit()
        else:
            # A lost worker cannot safely replay preparation or submission.
            row.status, row.billing_stopped_at = "stopping", row.billing_stopped_at or now()
            db.commit()
    commands = list(db.scalars(select(Command).where(Command.session_id == row.id, Command.status.in_(BUSY))))
    try:
        session_access(db, row).editor()
    except HTTPException:
        row.status, row.billing_stopped_at = "stopping", row.billing_stopped_at or now()
    expired = now() >= aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"])
    anchor = idle_anchor(db, row)
    idle = not commands and now() >= anchor+timedelta(seconds=row.snapshot["idle_seconds"])
    if expired or idle:
        row.status = "stopping"
        cutoff = aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]) if expired else anchor+timedelta(seconds=row.snapshot["idle_seconds"])
        row.billing_stopped_at = row.billing_stopped_at or cutoff
    if row.status != "stopping" and charge_at(row, now())[0] >= row.billed_seconds + 60:
        if not reconcile(db, row, now()):
            row.status, row.error = "stopping", "积分不足，云电脑正在结束"
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
        return
    maintain_preview(db, row, provider, False)
    reconcile(db, row, now(), final=True)
    row.status, row.ended_at = "ended", now()
    db.commit()
