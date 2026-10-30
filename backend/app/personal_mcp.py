"""Remote connections require explicit authorization of discovered definitions."""
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from . import mcp_client
from .auth import get_access
from .db import get_db
from .models import MCPServer
from .personal_skills import owned, audit
from .site_skills import Version

router = APIRouter(prefix="/agent/mcp", tags=["personal-mcp"])


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    url: str
    transport: Literal["streamable_http", "sse"] = "streamable_http"
    headers: dict[str, SecretStr] | None = None
    enabled: bool = False
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    acknowledge_external_access: bool = False


class ConnectionUpdate(ConnectionWrite):
    version: int = Field(ge=1, strict=True)


def read(row):
    return {"id": str(row.id), "name": row.name, "url": row.url, "transport": row.transport,
        "enabled": row.enabled, "version": row.version, "authorization_version": row.authorization_version,
        "has_credentials": bool(row.encrypted_headers), "header_names": row.header_names,
        "tools": row.tools, "allowed_tools": row.allowed_tools, "last_checked_at": row.last_checked_at}


def configure(row, payload):
    try:
        mcp_client.validate_url(payload.url)
        headers = {key: value.get_secret_value() for key, value in payload.headers.items()} if payload.headers is not None else None
        encrypted = mcp_client.encrypt_headers(headers) if headers is not None else row.encrypted_headers
        changed = (row.url != payload.url or row.transport != payload.transport
            or headers is not None and headers != mcp_client.decrypt_headers(row.encrypted_headers))
    except mcp_client.MCPError as exc:
        raise HTTPException(422, str(exc)) from None
    if changed:
        row.tools, row.allowed_tools, row.last_checked_at = [], [], None
        row.authorization_version += 1
    allowed = list(dict.fromkeys(payload.allowed_tools))
    known = {tool["name"] for tool in row.tools}
    if any(name not in known for name in allowed):
        raise HTTPException(422, "请先测试连接并重新选择工具")
    if allowed != row.allowed_tools:
        if set(allowed) - set(row.allowed_tools) and not payload.acknowledge_external_access:
            raise HTTPException(422, "请确认向此服务发送参数并自动执行所选工具")
        row.authorization_version += 1
    if payload.enabled and (not row.last_checked_at or not allowed):
        raise HTTPException(422, "请先测试连接并授权至少一个工具")
    row.name, row.url, row.transport = payload.name, payload.url, payload.transport
    row.encrypted_headers = encrypted
    if headers is not None:
        row.header_names = sorted(headers)
    row.allowed_tools, row.enabled = allowed, payload.enabled


@router.get("")
def listing(db=Depends(get_db), access=Depends(get_access)):
    return [read(row) for row in db.scalars(select(MCPServer).where(
        MCPServer.owner_id == access.user_id, MCPServer.workspace_id == access.workspace_id)
        .order_by(MCPServer.created_at.desc(), MCPServer.id))]


@router.post("", status_code=201)
def create(payload: ConnectionWrite, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = MCPServer(owner_id=access.user_id, workspace_id=access.workspace_id, version=1,
        authorization_version=1, tools=[], allowed_tools=[], header_names=[])
    configure(row, payload)
    db.add(row)
    db.flush()
    audit(db, access, request, "mcp_server.created", row)
    db.commit()
    return read(row)


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    return read(owned(db, MCPServer, identifier, access))


@router.put("/{identifier}")
def update(identifier: UUID, payload: ConnectionUpdate, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, MCPServer, identifier, access, True)
    if row.version != payload.version:
        raise HTTPException(409, "连接已更新，请重新加载后再保存")
    configure(row, payload)
    row.version += 1
    audit(db, access, request, "mcp_server.updated", row)
    db.commit()
    return read(row)


@router.post("/{identifier}/sync")
def sync(identifier: UUID, payload: Version, request: Request, db=Depends(get_db), access=Depends(get_access)):
    from datetime import datetime, timezone
    row = owned(db, MCPServer, identifier, access)
    if row.version != payload.version:
        raise HTTPException(409, "连接已更新，请重新加载后再测试")
    try:
        discovered = mcp_client.discover(row.url, row.transport, mcp_client.decrypt_headers(row.encrypted_headers))
    except mcp_client.MCPError as exc:
        raise HTTPException(502, str(exc)) from None
    # Discovery can take seconds. Do not overwrite a concurrent edit.
    db.expire_all()
    row = owned(db, MCPServer, identifier, access, True)
    if row.version != payload.version:
        raise HTTPException(409, "连接已更新，请重新加载后再测试")
    previous = {tool["name"]: mcp_client.tool_digest(tool) for tool in row.tools}
    row.allowed_tools = [tool["name"] for tool in discovered if tool["name"] in row.allowed_tools
        and previous.get(tool["name"]) == mcp_client.tool_digest(tool)]
    if row.tools != discovered:
        row.authorization_version += 1
    row.tools, row.last_checked_at = discovered, datetime.now(timezone.utc)
    if not row.allowed_tools:
        row.enabled = False
    row.version += 1
    audit(db, access, request, "mcp_server.synced", row)
    db.commit()
    return read(row)


@router.delete("/{identifier}", status_code=204)
def delete(identifier: UUID, payload: Version, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, MCPServer, identifier, access, True)
    if row.version != payload.version:
        raise HTTPException(409, "连接已更新，请重新加载后再删除")
    audit(db, access, request, "mcp_server.deleted", row)
    db.delete(row)
    db.commit()
