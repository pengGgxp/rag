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
BUSY = ("queued", "preparing", "running", "syncing", "save_queued")


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


def read(row):
    labels = {"starting": "正在启动云电脑", "ready": "云电脑运行中", "running": "正在执行", "stopping": "正在结束云电脑",
              "uncertain": "正在核对云电脑状态", "ended": "云电脑已结束", "failed": "云电脑未能启动"}
    seconds, estimated = charge_at(row, now())
    return {"id": str(row.id), "status": row.status, "summary": row.error or labels[row.status],
        "terminal": row.ended_at is not None, "credits": str(row.charged_credits if row.ended_at else min(estimated, row.charged_credits+row.held_credits)),
        "credits_per_minute": str(row.snapshot["credits_per_minute"]), "seconds": seconds,
        "idle_ends_at": (aware(row.last_activity_at)+timedelta(seconds=row.snapshot["idle_seconds"])).isoformat() if row.status == "ready" else None,
        "started_at": row.started_at, "ended_at": row.ended_at}


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
    return {"computer": read(row) if row else None}


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


@router.post("/cloud-computer/commands/{identifier}/retry-save")
def retry_save(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    command, row = own_command(db, access, identifier)
    if row.status != "ready" or row.ended_at or command.status != "save_failed":
        raise HTTPException(409, "当前环境已结束或正在执行，请查看资源库中的输出")
    command.status = "save_queued"
    command.heartbeat_at = now()
    db.commit()
    # Maintenance performs the transfer; the original command is never re-run.
    return command_read(command)


def submit(db, access, conversation_id, request_id, arguments, skill_packages=None):
    access = owner(db, access, conversation_id)
    access.editor()
    serialize_admission(db)
    payload = {"command": arguments["command"], "inputs": arguments.get("inputs", []), "skills": skill_packages or {}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(Command).join(Computer).where(Computer.workspace_id == access.workspace_id,
        Computer.user_id == access.user_id, Computer.conversation_id == conversation_id,
        Command.request_id == request_id, Command.digest == digest))
    if existing:
        if existing.status == "queued":
            dispatch(existing.id)
        return existing
    from .workspace_files import resolve_input
    from .storage_billing import check_capacity
    selected = [resolve_input(db, access, ref) for ref in arguments.get("inputs", [])]
    if sum(version.size_bytes for _, version in selected) > 512 * 1024 * 1024:
        raise HTTPException(413, "单次输入总计不能超过 512 MiB")
    if len({row.path for row, _ in selected}) != len(selected):
        raise HTTPException(422, "同一路径只能选择一个资源版本")
    check_capacity(db, access.workspace_id, 1)
    settings = db.get(Settings, 1, populate_existing=True)
    if not settings or not settings.enabled:
        raise HTTPException(503, "云电脑尚未开放，请联系管理员")
    row = db.scalar(select(Computer).where(Computer.user_id == access.user_id, Computer.ended_at.is_(None)).with_for_update())
    if row and (row.workspace_id != access.workspace_id or row.conversation_id != conversation_id):
        raise HTTPException(409, "你在其他会话有运行中的云电脑，请先结束后重试")
    if row and row.status in ("stopping", "uncertain"):
        raise HTTPException(409, "云电脑正在结束或核对状态，请稍后重试")
    if row is None:
        count = db.scalar(select(func.count()).select_from(Computer).where(Computer.ended_at.is_(None)))
        if count >= settings.config["concurrency"]:
            raise HTTPException(409, "云电脑繁忙，请稍后重试")
        identifier = uuid4()
        snapshot = {**settings.config, "version": settings.version}
        if settings.config.get("oss_role_arn"):
            from .storage_backend import active_settings
            storage = active_settings(db)
            if not storage or storage.provider != "oss":
                raise HTTPException(503, "资源挂载尚未就绪，请联系管理员")
            snapshot["resource_mount"] = {"bucket": storage.bucket, "region": storage.region,
                "prefix": f"workspaces/{access.workspace_id}/cloud-computer/{identifier}/workspace/"}
            mount_resources(db, access, arguments.get("inputs", []))
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
    if db.scalar(select(Command.id).where(Command.session_id == row.id, Command.status == "save_failed")):
        raise HTTPException(409, "请先重试保存未完成的文件，或结束云电脑后再执行")
    command = Command(id=uuid4(), session_id=row.id, request_id=request_id, digest=digest, payload=payload,
                      status="queued", cancel_requested=False, result={})
    db.add(command)
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
    """Resolve a workspace snapshot, with explicit fixed versions taking precedence."""
    from .workspace_files import WorkspaceFile, _current_file_version, resolve_input, valid_path
    selected = {}
    for resource in db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.deleted_at.is_(None)).order_by(WorkspaceFile.path)):
        valid_path(resource.path)
        selected[resource.path] = (resource, _current_file_version(db, resource))
    for reference in explicit:
        resource, version = resolve_input(db, access, reference)
        selected[resource.path] = (resource, version)
    if len(selected) > 2000 or sum(version.size_bytes for _, version in selected.values()) > 512 * 1024 * 1024:
        raise HTTPException(413, "资源库超过当前云电脑容量，请联系管理员调整运行环境")
    return list(selected.values())


def mount_storage(db, row):
    from .storage_backend import current_storage
    storage = current_storage(db)
    mount = row.snapshot["resource_mount"]
    if getattr(storage, "bucket", None) != mount["bucket"] or getattr(storage, "region", None) != mount["region"]:
        raise HTTPException(409, "文件存储已变化，请先结束当前云电脑")
    return storage


def stage_mount(db, row, access, explicit, provider=None, sandbox=None):
    """Meter/cache preparation once; never mount immutable resource objects writable."""
    import io
    from .resource_cache import read_bytes
    resources = mount_resources(db, access, explicit)
    storage = mount_storage(db, row)
    manifest = dict(row.manifest)
    visible = {resource.path for resource, _ in resources}
    for path in list(manifest):
        if path not in visible:
            if sandbox is not None:
                provider.remove(sandbox, path)
            else:
                storage.delete(row.snapshot["resource_mount"]["prefix"] + path)
            manifest.pop(path)
    for resource, version in resources:
        prior = manifest.get(resource.path, {})
        if prior.get("id") == str(resource.id) and prior.get("version") == version.version:
            continue
        data = read_bytes(db, access, resource, version)
        if sandbox is not None:
            provider.write(sandbox, resource.path, data)
        else:
            storage.put(row.snapshot["resource_mount"]["prefix"] + resource.path,
                io.BytesIO(data), resource.content_type)
        manifest[resource.path] = {"id": str(resource.id), "version": version.version, "sha256": version.content_hash}
        row.manifest = dict(manifest)
        db.commit()
    row.manifest = manifest
    db.commit()


def cleanup_mount(db, row):
    if not row.snapshot.get("resource_mount") or row.error:
        return
    try:
        storage = mount_storage(db, row)
        prefix = row.snapshot["resource_mount"]["prefix"]
        for item in storage.iter_objects(prefix):
            if not item["Key"].startswith(prefix):
                raise RuntimeError("mount object escaped its session prefix")
            storage.delete(item["Key"])
        row.snapshot = {**row.snapshot, "mount_cleaned": True}
        db.commit()
    except Exception:
        logger.warning("Cloud mount cleanup deferred: %s", row.id, exc_info=False)


def synchronize(db, row, provider, sandbox):
    from .workspace_files import _save_bytes, valid_path, WorkspaceFile
    access = session_access(db, row)
    access.editor()
    manifest = dict(row.manifest)
    changed = []
    for path, entry in provider.manifest(sandbox).items():
        valid_path(path)
        prior = manifest.get(path, {})
        if prior.get("sha256") == entry["sha256"]:
            continue
        current_file = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == row.workspace_id,
            WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))
        if prior.get("id") and (not current_file or str(current_file.id) != prior["id"] or current_file.current_version != prior["version"]):
            raise HTTPException(409, "资源已被修改或删除，请重新选择后处理")
        if current_file and not prior.get("id"):
            raise HTTPException(409, "输出文件与已有资源重名，请使用新文件名")
        data = provider.read(sandbox, path, entry["sha256"])
        saved = _save_bytes(row.workspace_id, access, db, path, data,
            current_file.content_type if current_file else "application/octet-stream", prior.get("version"))
        manifest[path] = {"id": saved["id"], "version": saved["version"], "sha256": entry["sha256"]}
        row.manifest = dict(manifest)
        db.commit()
        changed.append(saved)
    return changed


def recover_mount(db, row):
    """A mounted working copy survives VM expiry; recover without re-executing."""
    from .workspace_files import valid_path
    storage = mount_storage(db, row)
    prefix = row.snapshot["resource_mount"]["prefix"]
    objects = {}
    total = 0
    for item in storage.iter_objects(prefix):
        if not item["Key"].startswith(prefix):
            raise RuntimeError("mount object escaped its session prefix")
        path = item["Key"][len(prefix):]
        if not path or path.endswith("/"):
            continue
        valid_path(path)
        if any(part in {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"} for part in path.split("/")):
            continue
        total += item["Size"]
        if item["Size"] > 200 * 1024 * 1024 or total > 512 * 1024 * 1024 or len(objects) >= 2000:
            raise HTTPException(413, "文件超出当前云电脑容量，请联系管理员恢复")
        with storage.download(item["Key"]) as source:
            digest, size = hashlib.sha256(), 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                if size > item["Size"]:
                    raise RuntimeError("mount recovery size mismatch")
                digest.update(block)
        if size != item["Size"]:
            raise RuntimeError("mount recovery size mismatch")
        objects[path] = {"sha256": digest.hexdigest(), "size": size, "key": item["Key"]}
    class Recovered:
        def manifest(self, _sandbox):
            return objects
        def read(self, _sandbox, path, expected):
            with storage.download(objects[path]["key"]) as source:
                data = source.read(objects[path]["size"] + 1)
            if len(data) != objects[path]["size"] or hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError("mount recovery content changed")
            return data
    return synchronize(db, row, Recovered(), None)


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
            if not row.provider_id:
                if row.snapshot.get("resource_mount"):
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
            from .workspace_files import resolve_input
            from .resource_cache import read_bytes
            manifest = dict(row.manifest)
            for path, previous in list(manifest.items()):
                try:
                    resolve_input(db, access, {"id": previous["id"], "version": previous["version"]})
                except HTTPException:
                    provider.remove(sandbox, path)
                    manifest.pop(path)
            if row.snapshot.get("resource_mount"):
                stage_mount(db, row, access, command.payload["inputs"], provider, sandbox)
                manifest = dict(row.manifest)
            for reference in ([] if row.snapshot.get("resource_mount") else command.payload["inputs"]):
                resource, version = resolve_input(db, access, reference)
                prior = manifest.get(resource.path, {})
                if prior.get("id") == str(resource.id) and prior.get("version") == version.version:
                    continue
                provider.write(sandbox, resource.path, read_bytes(db, access, resource, version))
                manifest[resource.path] = {"id": str(resource.id), "version": version.version, "sha256": version.content_hash}
            for skill_id, package in command.payload["skills"].items():
                for path, encoded in package.items():
                    provider.write_skill(sandbox, f"{skill_id}/{path}", base64.b64decode(encoded))
            row.manifest = manifest
            db.refresh(command)
            db.refresh(row, ["status", "billing_stopped_at"])
            if command.cancel_requested or row.status == "stopping":
                command.status, command.finished_at = "cancelled", now()
                command.result = {"status": "cancelled", "error": "命令已停止"}
                db.commit()
                return
            row.status = "running"
            command.status = "running"
            db.commit()
            submitted = True
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
                    if result["exit_code"] != 0:
                        command.result = {**command.result, "error": "命令未能完成，请根据执行结果调整后再试", "error_code": "script_failed"}
                    command.status = "syncing"
                    db.commit()
                    break
                command.heartbeat_at = now()
                db.commit()
                time.sleep(1)
            files = synchronize(db, row, provider, sandbox)
            command.result = {**command.result, "generated_files": files}
            command.status = command.result["status"]
        except Exception:
            db.rollback()
            db.refresh(command)
            db.refresh(row)
            if command.status == "syncing":
                command.result = {**command.result, "error": "命令已运行，文件尚未全部保存，请重试保存，勿重复执行"}
                command.status = "save_failed"
            else:
                command.status = "uncertain" if submitted else "failed"
                command.result = {"status": command.status, "error": "执行结果待核对，请查看已有输出，勿重复执行" if submitted else "云电脑未能准备文件，请检查资源与额度后重试",
                                  "uncertain": submitted}
                if submitted:
                    row.status = "stopping"
                    row.billing_stopped_at = now()
                elif row.provider_id:
                    row.status = "stopping"
                    row.billing_stopped_at = row.started_at
                else:
                    reconcile(db, row, now(), final=True)
                    row.status, row.ended_at = "failed", now()
                    db.commit()
                    cleanup_mount(db, row)
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
                command.cancel_requested = True
                db.commit()
                raise
        time.sleep(.5)
        db.refresh(command)
    row = db.get(Computer, command.session_id, populate_existing=True)
    return {**command.result, "cloud_computer": read(row), "command_id": str(command.id)}


def maintenance(factory=SessionLocal):
    """Reconcile lifetime independently of browser/Agent, without replaying commands."""
    with factory() as db:
        identifiers = list(db.scalars(select(Computer.id).where(Computer.ended_at.is_(None))))
    for identifier in identifiers:
        try:
            maintain_one(identifier, factory)
        except Exception:
            logger.warning("Cloud computer reconciliation deferred: %s", identifier, exc_info=False)
    with factory() as db:
        for row in db.scalars(select(Computer).where(Computer.ended_at.is_not(None), Computer.error.is_(None))):
            if row.snapshot.get("resource_mount") and not row.snapshot.get("mount_cleaned"):
                cleanup_mount(db, row)


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
    if not alive:
        if row.snapshot.get("resource_mount"):
            try:
                recover_mount(db, row)
            except Exception:
                db.rollback()
                db.refresh(row)
                row.error = "部分文件未能保存，请联系管理员恢复"
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
        cleanup_mount(db, row)
        return
    recoverable = [command for command in commands if command.status == "save_queued" or
        (command.status != "queued" and command.heartbeat_at and (now()-aware(command.heartbeat_at)).total_seconds() > 45)]
    for command in recoverable:
        if db.get_bind().dialect.name == "postgresql":
            with db.get_bind().connect() as connection:
                lock = int.from_bytes(command.id.bytes[-8:], "big", signed=True)
                if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}):
                    continue
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
        sandbox = provider.connect(row.provider_id)
        result = provider.poll(sandbox, command.id) if command.status != "preparing" else None
        if command.status in ("syncing", "save_queued") or result is not None:
            try:
                files = synchronize(db, row, provider, sandbox)
                command.result = {**(result or command.result), "generated_files": files}
                command.result.pop("error", None)
                command.status = "completed" if command.result.get("exit_code") == 0 else "failed"
                command.result["status"] = command.status
            except Exception:
                db.rollback()
                db.refresh(command)
                command.status = "save_failed"
                command.result = {**command.result, "error": "文件尚未全部保存，请重试保存，勿重复执行"}
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
    idle = not commands and now() >= aware(row.last_activity_at)+timedelta(seconds=row.snapshot["idle_seconds"])
    if expired or idle:
        row.status = "stopping"
        cutoff = aware(row.started_at)+timedelta(seconds=row.snapshot["max_seconds"]) if expired else aware(row.last_activity_at)+timedelta(seconds=row.snapshot["idle_seconds"])
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
    try:
        if sandbox is None:
            raise RuntimeError("sandbox unavailable")
        synchronize(db, row, provider, sandbox)
    except Exception:
        db.rollback()
        db.refresh(row)
        row.error = "部分文件未能保存，请查看资源库；需要帮助请联系管理员"
        logger.warning("Cloud output final synchronization failed: %s", identifier, exc_info=False)
    if not provider.kill(row.provider_id):
        db.commit()
        logger.error("Cloud computer destruction not confirmed: %s", identifier)
        return
    reconcile(db, row, now(), final=True)
    row.status, row.ended_at = "ended", now()
    db.commit()
    cleanup_mount(db, row)
