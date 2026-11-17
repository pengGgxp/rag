"""Registered HTTP services. Browser visibility renews a bounded idle lease."""
import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, update

from . import cloud_computer as cloud
from .auth import Access, aware, get_access, now
from .config import get_settings
from .db import get_db
from .models import CloudPreview as Preview, CloudPreviewGrant as Grant, ChatMessage

router = APIRouter(tags=["cloud-preview"])
LABELS = {"start_preview": "启动成果预览", "stop_preview": "停止成果预览"}


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    command: str = Field(min_length=1, max_length=16000)
    port: int = Field(ge=1024, le=49151)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("port")
    @classmethod
    def valid_port(cls, value):
        if value in {2049, 2375, 2376, 3306, 5432, 5900, 6379, 8001, 8082, 11211}:
            raise ValueError("该端口用于管理或数据服务，请为网页选择其他端口")
        return value


TOOLS = [
    {"type": "function", "function": {"name": "start_preview",
        "description": "在指定 computer_id 启动 HTTP 网页服务。命令保持前台运行，监听 0.0.0.0 和指定端口；本工具管理后台进程和就绪检查，不创建云电脑。同一实例一个预览服务，换服务先 stop_preview。就绪后交付真实预览入口，关闭预览不结束计费。",
        "parameters": {**Start.model_json_schema(), "properties": {**Start.model_json_schema()["properties"],
            "computer_id": {"type": "string", "format": "uuid"}}, "required": ["computer_id", "command", "port", "name"]}}},
    {"type": "function", "function": {"name": "stop_preview",
        "description": "停止指定 computer_id 的预览服务，保留文件和云电脑；不影响其他实例。",
        "parameters": {"type": "object", "properties": {"computer_id": {"type": "string", "format": "uuid"}},
            "required": ["computer_id"], "additionalProperties": False}}},
]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def origin(row):
    domain = get_settings().preview_domain
    if not domain or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in domain):
        raise HTTPException(503, "预览尚未开放，请联系管理员")
    return f"https://{row.id.hex}.{domain}"


def read(row):
    labels = {"starting": "正在启动预览", "ready": "预览已就绪", "stopping": "正在停止预览",
        "stopped": "预览已停止", "failed": "预览未能启动", "uncertain": "正在核对预览状态"}
    return {"id": str(row.id), "computer_id": str(row.session_id), "name": row.name, "status": row.status,
        "summary": row.error or labels.get(row.status, "预览不可用"), "terminal": row.ended_at is not None,
        "url": f"{get_settings().frontend_url.rstrip('/')}/previews/{row.id}" if row.status == "ready" else None}


def owned(db, access, identifier, live=False):
    row = db.get(Preview, identifier)
    computer = db.get(cloud.Computer, row.session_id) if row else None
    if not computer or computer.user_id != access.user_id or computer.workspace_id != access.workspace_id:
        raise HTTPException(404, "预览不存在")
    cloud.owned_computer(db, access, computer.id)
    if live:
        access.editor()
        if not get_settings().preview_enabled:
            raise HTTPException(503, "成果预览尚未开放，请联系管理员")
    if live and (row.status != "ready" or row.ended_at or computer.ended_at or computer.status in ("stopping", "uncertain")
            or computer.billing_stopped_at or not computer.started_at
            or now() >= aware(computer.started_at) + timedelta(seconds=computer.snapshot["max_seconds"])):
        raise HTTPException(410, "预览已结束，请查看已保存的文件")
    return row, computer


@router.get("/cloud-computers/{identifier}/preview")
def current(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    computer = cloud.owned_computer(db, access, identifier)
    row = db.scalar(select(Preview).where(Preview.session_id == computer.id)
        .order_by(Preview.created_at.desc()))
    return {"preview": read(row) if row else None}


@router.get("/previews/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row, computer = owned(db, access, identifier)
    return {"preview": read(row), "computer": cloud.read(computer, db), "conversation_id": str(computer.conversation_id)}


@router.post("/previews/{identifier}/access")
def authorize(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row, computer = owned(db, access, identifier, live=True)
    token = secrets.token_urlsafe(32)
    db.add(Grant(digest=digest(token), preview_id=row.id, session_hash=access.session_hash,
        kind="ticket", expires_at=now() + timedelta(seconds=60)))
    db.commit()
    return {"url": f"{origin(row)}/_zk_preview/auth?ticket={token}", "expires_in": 60}


@router.post("/previews/{identifier}/heartbeat")
def heartbeat(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row, computer = owned(db, access, identifier, live=True)
    # Serialize with session end and billing maintenance. Multiple windows share this lease.
    computer = db.get(cloud.Computer, computer.id, with_for_update=True, populate_existing=True)
    if computer.status == "stopping" or computer.billing_stopped_at or computer.ended_at:
        raise HTTPException(410, "云电脑正在结束")
    row.lease_until = min(now() + timedelta(seconds=60), aware(computer.started_at) + timedelta(seconds=computer.snapshot["max_seconds"]))
    computer.last_activity_at = now()
    db.execute(update(Grant).where(Grant.preview_id == row.id, Grant.session_hash == access.session_hash,
        Grant.kind == "cookie", Grant.expires_at > now()).values(expires_at=now()+timedelta(seconds=120)))
    db.commit()
    return {"lease_until": row.lease_until.isoformat(), "computer": cloud.read(computer, db)}


def attach(db, computer, command, arguments):
    active = db.scalar(select(Preview).where(Preview.session_id == computer.id, Preview.ended_at.is_(None)))
    if active:
        raise HTTPException(409, "当前会话已有预览，请先停止原预览后再启动")
    row = Preview(session_id=computer.id, command_id=command.id, name=arguments["name"], port=arguments["port"], status="starting")
    db.add(row)
    db.flush()
    return row


def finish_command(db, command, computer, provider, sandbox):
    row = db.scalar(select(Preview).where(Preview.command_id == command.id))
    if not row:
        return
    if row.ended_at or row.status == "stopping":
        provider.stop_preview(sandbox, command.id)
        row.status, row.ended_at = "stopped", now()
        command.result = {**command.result, "preview": read(row)}
        return
    if command.status == "completed":
        from .site_auth import protect
        host, token = provider.preview_target(sandbox, row.port, row.command_id)
        if not token:
            raise RuntimeError("provider did not supply authenticated HTTP traffic")
        row.upstream_host, row.encrypted_traffic_token = host, protect(token)
        row.status = "ready"
        row.error = None
    else:
        provider.stop_preview(sandbox, command.id)
        row.status = "failed" if command.status != "uncertain" else "uncertain"
        row.error = "网页服务未就绪，请检查启动命令和端口后重试" if row.status == "failed" else "启动结果待核对，请勿重复启动"
        row.ended_at = now() if row.status == "failed" else None
    command.result = {**command.result, "preview": read(row), "summary": read(row)["summary"]}


def stop(db, access, row, computer):
    access.editor()
    computer = db.get(cloud.Computer, computer.id, with_for_update=True, populate_existing=True)
    if row.ended_at:
        return read(row)
    row.status, row.lease_until = "stopping", None
    db.commit()
    try:
        provider = cloud.adapter(computer.snapshot, computer.encrypted_api_key)
        if computer.provider_id and provider.exists(computer.provider_id):
            provider.stop_preview(provider.connect(computer.provider_id), row.command_id)
    except Exception:
        row.error = "正在核对服务停止状态，请稍后刷新"
        db.commit()
        return read(row)
    row.status, row.ended_at = "stopped", now()
    computer.last_activity_at = now()
    db.commit()
    return read(row)


def maintain(db, computer, provider, alive):
    db.execute(delete(Grant).where(Grant.expires_at <= now()))
    row = db.scalar(select(Preview).where(Preview.session_id == computer.id, Preview.ended_at.is_(None)))
    if not row:
        return
    if row.status == "starting":
        command = db.get(cloud.Command, row.command_id)
        if command.status in {"failed", "cancelled", "uncertain"}:
            if command.status == "uncertain" and computer.provider_id:
                row.status = "uncertain"
            else:
                row.status, row.ended_at = "failed", now()
                row.error = "网页服务未能启动，请检查云电脑状态后重试"
    if not alive or computer.ended_at or computer.status == "stopping":
        row.status, row.ended_at, row.lease_until = "stopped", now(), None
    elif row.status in ("ready", "stopping", "uncertain"):
        sandbox = provider.connect(computer.provider_id)
        if row.status == "stopping":
            provider.stop_preview(sandbox, row.command_id)
            row.status, row.ended_at = "stopped", now()
        elif row.status == "ready" and not provider.preview_alive(sandbox, row.command_id):
            row.status, row.ended_at, row.lease_until = "failed", now(), None
            row.error = "网页服务已退出，请在对话中修正后重新启动"
        elif row.status == "uncertain":
            command = db.get(cloud.Command, row.command_id)
            result = provider.poll(sandbox, command.id)
            if result is not None:
                command.result = result
                command.status = "completed" if result.get("exit_code") == 0 else "failed"
                finish_command(db, command, computer, provider, sandbox)


def idle_anchor(db, computer):
    # Heartbeats already record last_activity_at; the access lease is not extra idle time.
    return aware(computer.last_activity_at)


def execute(runtime, name, arguments, check_active, *, call_id=None, **execution):
    with runtime.factory() as db:
        access = runtime.access.revalidate(db)
        message = db.get(ChatMessage, runtime.message_id)
        if not message:
            raise HTTPException(404, "当前会话不可用")
        cloud.owner(db, access, message.conversation_id).editor()
        computer = cloud.owned_computer(db, access, arguments.get("computer_id"), write=True)
        if name == "stop_preview":
            if set(arguments) != {"computer_id"}:
                raise HTTPException(422, "请指定要停止预览的 computer_id")
            row = db.scalar(select(Preview).where(Preview.session_id == computer.id, Preview.ended_at.is_(None)))
            return {"computer_id": str(computer.id),
                **({"preview": stop(db, access, row, computer)} if row else {"summary": "预览已停止"})}
        if not get_settings().preview_enabled:
            raise HTTPException(503, "成果预览尚未开放，请联系管理员")
        payload = Start.model_validate({key: value for key, value in arguments.items() if key != "computer_id"}).model_dump()
        return cloud.execute(db, access, message.conversation_id, cloud.tool_request_id(runtime.request_id, call_id),
            {"command": payload["command"], "preview": payload, "computer_id": str(computer.id)}, check_active, **execution)
