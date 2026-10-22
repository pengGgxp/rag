"""User-owned MCP connections and progressively loaded Agent Skills."""

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import frontmatter
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from . import mcp_client, skill_files
from .auth import Access, get_access
from .audit_log import record_audit
from .db import get_db
from .models import AgentSkill, AgentSkillFile, MCPServer

router = APIRouter(prefix="/agent", tags=["agent-extensions"])


class SkillFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    content_base64: str | None = Field(default=None, max_length=7 * 1024 * 1024)


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markdown: str = Field(min_length=1, max_length=skill_files.MAX_FILE_BYTES)
    enabled: bool = True
    files: list[SkillFileInput] | None = Field(default=None, max_length=skill_files.MAX_FILES - 1)


class MCPInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    url: str
    transport: Literal["streamable_http", "sse"] = "streamable_http"
    headers: dict[str, SecretStr] | None = None
    enabled: bool = True
    allowed_tools: list[str] = Field(default_factory=list)
    acknowledge_external_access: bool = False


def parse_skill(markdown: str):
    try:
        post = frontmatter.loads(markdown)
    except Exception as exc:
        raise HTTPException(422, "SKILL.md frontmatter 无法解析") from exc
    name, description = post.get("name"), post.get("description")
    if (
        not isinstance(name, str) or not 1 <= len(name) <= 64 or name != name.lower()
        or name.startswith("-") or name.endswith("-") or "--" in name
        or any(not (c.isalnum() or c == "-") for c in name)
    ):
        raise HTTPException(422, "Skill name 必须是 1–64 位小写字母、数字或单连字符")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise HTTPException(422, "Skill description 必须是 1–1024 字符的说明")
    for field, limit in (("license", None), ("compatibility", 500), ("allowed-tools", None)):
        if field in post and (not isinstance(post[field], str) or (
            limit and not 1 <= len(post[field]) <= limit
        )):
            raise HTTPException(422, f"Skill {field} 格式无效")
    if "metadata" in post and (
        not isinstance(post["metadata"], dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in post["metadata"].items())
    ):
        raise HTTPException(422, "Skill metadata 必须是字符串键值映射")
    return name, description.strip(), post.content


def apply_skill(row, payload):
    name, description, _ = parse_skill(payload.markdown)
    existing = {item.path: item for item in row.files}
    files = {"SKILL.md": payload.markdown.encode("utf-8")}
    if payload.files is None:
        files.update({path: item.content for path, item in existing.items()})
    else:
        seen = {"skill.md"}
        for item in payload.files:
            path = skill_files.safe_path(item.path)
            if path.casefold() in seen:
                raise HTTPException(422, "Skill 存在重复文件路径")
            seen.add(path.casefold())
            if item.content_base64 is None:
                if path not in existing:
                    raise HTTPException(422, "新文件必须提供内容")
                files[path] = existing[path].content
            else:
                try:
                    files[path] = base64.b64decode(item.content_base64, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise HTTPException(422, "文件内容不是有效的 Base64") from exc
    skill_files.validate_files(files)
    # Validate the whole replacement before mutating any persistent state.
    row.name, row.description, row.markdown, row.enabled = name, description, payload.markdown, payload.enabled
    row.files = [existing.get(path) or AgentSkillFile(path=path, content=content)
                 for path, content in files.items() if path != "SKILL.md"]
    for item in row.files:
        item.content = files[item.path]
    row.updated_at = datetime.now(timezone.utc)


def owned(db, model, identifier: UUID, access: Access):
    row = db.scalar(select(model).where(
        model.id == identifier, model.owner_id == access.user_id,
        model.workspace_id == access.workspace_id,
    ).execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "扩展不存在或无权访问")
    return row


def rows(db, model, access, identifiers=None):
    statement = select(model).where(
        model.owner_id == access.user_id, model.workspace_id == access.workspace_id,
    )
    if identifiers is not None:
        statement = statement.where(model.id.in_(identifiers))
    return db.scalars(statement.order_by(model.created_at, model.id)).all()


def skill_read(row, *, detail=False):
    data = {
        "id": str(row.id), "name": row.name, "description": row.description,
        "enabled": row.enabled, "updated_at": row.updated_at.isoformat(),
    }
    if detail:
        data["markdown"] = row.markdown
        data["files"] = skill_files.manifest(skill_files.bundle_files(row))
    return data


def server_read(row):
    return {
        "id": str(row.id), "name": row.name, "url": row.url,
        "transport": row.transport, "enabled": row.enabled,
        "has_credentials": bool(row.encrypted_headers), "header_names": row.header_names,
        "tools": row.tools, "allowed_tools": row.allowed_tools,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
    }


def audit(db, access, request, action, row):
    record_audit(
        db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action=action, resource_type="agent_skill" if isinstance(row, AgentSkill) else "mcp_server",
        resource_id=row.id, metadata={"name": row.name}, request=request,
    )


@router.get("/skills")
def list_skills(db=Depends(get_db), access=Depends(get_access)):
    return [skill_read(item) for item in rows(db, AgentSkill, access)]


@router.post("/skills", status_code=201)
def create_skill(payload: SkillInput, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = AgentSkill(
        workspace_id=access.workspace_id, owner_id=access.user_id,
    )
    apply_skill(row, payload)
    db.add(row)
    db.flush()
    audit(db, access, request, "agent_skill.created", row)
    db.commit()
    db.refresh(row)
    return skill_read(row, detail=True)


@router.post("/skills/import", status_code=201)
async def import_skill(
    request: Request, files: list[UploadFile] = File(...), paths: list[str] | None = Form(None),
    db=Depends(get_db), access=Depends(get_access),
):
    if not files or len(files) > skill_files.MAX_FILES or paths is not None and len(paths) != len(files):
        raise HTTPException(422, "Skill 文件数量或路径数量无效")
    try:
        if len(files) == 1 and (files[0].filename or "").lower().endswith(".zip") and paths is None:
            data = await files[0].read(skill_files.MAX_UPLOAD_BYTES + 1)
            contents, folder = skill_files.unzip(data)
        else:
            entries, total = [], 0
            for index, file in enumerate(files):
                data = await file.read(skill_files.MAX_FILE_BYTES + 1)
                total += len(data)
                if len(data) > skill_files.MAX_FILE_BYTES or total > skill_files.MAX_BUNDLE_BYTES:
                    raise HTTPException(413, "Skill 文件超出大小限制")
                entries.append((paths[index] if paths else file.filename or "", data))
            contents, folder = skill_files.imported_files(entries)
    finally:
        for file in files:
            await file.close()
    markdown = contents["SKILL.md"].decode("utf-8")
    name, _, _ = parse_skill(markdown)
    if folder is not None and folder != name:
        raise HTTPException(422, "Skill 目录名称必须与 frontmatter 的 name 一致")
    return create_skill(SkillInput(markdown=markdown, files=[
        SkillFileInput(path=path, content_base64=base64.b64encode(data).decode())
        for path, data in contents.items() if path != "SKILL.md"
    ]), request, db, access)


@router.get("/skills/{identifier}")
def get_skill(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    return skill_read(owned(db, AgentSkill, identifier, access), detail=True)


@router.put("/skills/{identifier}")
def update_skill(identifier: UUID, payload: SkillInput, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    apply_skill(row, payload)
    audit(db, access, request, "agent_skill.updated", row)
    db.commit()
    db.refresh(row)
    return skill_read(row, detail=True)


@router.get("/skills/{identifier}/file")
def get_skill_file(identifier: UUID, path: str, download: bool = False, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    path = skill_files.safe_path(path)
    content = skill_files.bundle_files(row).get(path)
    if content is None:
        raise HTTPException(404, "Skill 文件不存在")
    if not download:
        return {**skill_files.manifest({path: content})[0], "content": skill_files.text_content(content)}
    return Response(
        content, media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.split('/')[-1], safe='')}",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.get("/skills/{identifier}/export")
def export_skill(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    return Response(
        skill_files.export_zip(row.name, skill_files.bundle_files(row)), media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.name, safe='')}.zip"},
    )


@router.delete("/skills/{identifier}", status_code=204)
def delete_skill(identifier: UUID, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    audit(db, access, request, "agent_skill.deleted", row)
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.get("/mcp")
def list_servers(db=Depends(get_db), access=Depends(get_access)):
    return [server_read(item) for item in rows(db, MCPServer, access)]


def apply_server(row, payload):
    try:
        url = mcp_client.validate_url(payload.url)
        changed = (row.url, row.transport) != (url, payload.transport)
        available = {item["name"] for item in (row.tools or [])} if not changed else set()
        requested = set(payload.allowed_tools)
        if not requested.issubset(available):
            raise HTTPException(422, "工具尚未同步或地址已变化，请先保存并重新同步")
        if requested - set(row.allowed_tools or []) and not payload.acknowledge_external_access:
            raise HTTPException(422, "请确认允许向该服务发送参数并执行所选工具")
        if changed:
            row.tools, row.allowed_tools, row.last_checked_at = [], [], None
        row.name, row.url, row.transport = payload.name.strip(), url, payload.transport
        if not row.name:
            raise HTTPException(422, "连接名称不能为空")
        row.enabled, row.allowed_tools = payload.enabled, list(dict.fromkeys(payload.allowed_tools))
        if payload.headers is not None:
            headers = {key: value.get_secret_value() for key, value in payload.headers.items()}
            row.encrypted_headers = mcp_client.encrypt_headers(headers)
            row.header_names = list(headers)
    except mcp_client.MCPError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/mcp", status_code=201)
def create_server(payload: MCPInput, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = MCPServer(workspace_id=access.workspace_id, owner_id=access.user_id)
    apply_server(row, payload)
    db.add(row)
    db.flush()
    audit(db, access, request, "mcp_server.created", row)
    db.commit()
    db.refresh(row)
    return server_read(row)


@router.put("/mcp/{identifier}")
def update_server(identifier: UUID, payload: MCPInput, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, MCPServer, identifier, access)
    apply_server(row, payload)
    audit(db, access, request, "mcp_server.updated", row)
    db.commit()
    return server_read(row)


@router.post("/mcp/{identifier}/sync")
def sync_server(identifier: UUID, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, MCPServer, identifier, access)
    previous = {item["name"]: item for item in row.tools}
    snapshot = (row.url, row.transport, row.encrypted_headers)
    try:
        tools = mcp_client.discover(row.url, row.transport, mcp_client.decrypt_headers(row.encrypted_headers))
        for tool in tools:
            mcp_client.validate_tool(tool)
        if len({tool["name"] for tool in tools}) != len(tools):
            raise mcp_client.MCPError("MCP 服务返回了重复的工具名称")
    except (mcp_client.MCPError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, mcp_client.MCPError) else "MCP 工具参数定义无效"
        raise HTTPException(502, detail) from exc
    db.refresh(row)
    access.revalidate(db)
    if (row.url, row.transport, row.encrypted_headers) != snapshot:
        raise HTTPException(409, "连接配置已变化，请重新同步")
    row.allowed_tools = [
        tool["name"] for tool in tools if tool["name"] in row.allowed_tools
        and previous.get(tool["name"]) == tool
    ]
    row.tools, row.last_checked_at = tools, datetime.now(timezone.utc)
    audit(db, access, request, "mcp_server.synced", row)
    db.commit()
    return server_read(row)


@router.delete("/mcp/{identifier}", status_code=204)
def delete_server(identifier: UUID, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, MCPServer, identifier, access)
    audit(db, access, request, "mcp_server.deleted", row)
    db.delete(row)
    db.commit()
    return Response(status_code=204)


def validate_selection(db, access, skill_ids, server_ids):
    for model, identifiers in ((AgentSkill, skill_ids), (MCPServer, server_ids)):
        for identifier in identifiers:
            if not owned(db, model, UUID(str(identifier)), access).enabled:
                raise HTTPException(409, "选中的扩展已停用")


LOAD_SKILL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load an enabled user-selected skill's full task instructions when its description matches the task.",
        "parameters": {
            "type": "object", "properties": {"skill_id": {"type": "string", "format": "uuid"}},
            "required": ["skill_id"], "additionalProperties": False,
        },
    },
}

READ_SKILL_FILE = {
    "type": "function",
    "function": {
        "name": "read_skill_file",
        "description": "Read a selected skill's supporting UTF-8 file by its root-relative path. Read only relevant references, scripts or text assets; binary files return metadata, not decoded content. Does not execute code.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "format": "uuid"},
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "start_character": {"type": "integer", "minimum": 0, "description": "Continuation offset within start_line, from a prior next_character."},
                "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["skill_id", "path"], "additionalProperties": False,
        },
    },
}

SKILL_TOOLS = {"load_skill": LOAD_SKILL, "read_skill_file": READ_SKILL_FILE}


class ExtensionRuntime:
    def __init__(self, factory, access, skill_ids, server_ids):
        self.factory, self.access = factory, access
        self.skill_ids, self.server_ids = set(map(str, skill_ids)), set(map(str, server_ids))
        self.bindings = {}
        self.tools, self.skills = [], []
        with factory() as db:
            checked = access.revalidate(db)
            for row in rows(db, AgentSkill, checked, [UUID(item) for item in self.skill_ids]):
                if row.enabled:
                    self.skills.append(skill_read(row))
            if self.skills:
                self.tools.extend(SKILL_TOOLS.values())
            for row in rows(db, MCPServer, checked, [UUID(item) for item in self.server_ids]):
                if not row.enabled:
                    continue
                for tool in row.tools:
                    if tool["name"] not in row.allowed_tools:
                        continue
                    alias = f"mcp_{row.id.hex}_{mcp_client.tool_digest({'name': tool['name']})[:20]}"
                    self.bindings[alias] = (row.id, tool, row.name)
                    self.tools.append({
                        "type": "function", "function": {
                            "name": alias,
                            "description": f"{row.name} / {tool['name']}: {tool.get('description', '')}",
                            "parameters": tool["inputSchema"],
                        },
                    })

    def label(self, name):
        if name in SKILL_TOOLS:
            return "加载 Skill" if name == "load_skill" else "读取 Skill 文件"
        binding = self.bindings.get(name)
        return f"{binding[2]} · {binding[1]['name']}" if binding else name

    def execute(self, name, arguments, check_active=None):
        try:
            with self.factory() as db:
                checked = self.access.revalidate(db)
                if name in SKILL_TOOLS:
                    if check_active:
                        check_active()
                    Draft202012Validator(SKILL_TOOLS[name]["function"]["parameters"]).validate(arguments)
                    identifier = str(UUID(str(arguments.get("skill_id"))))
                    if identifier not in self.skill_ids:
                        return {"error": "该 Skill 不在本次对话授权范围内"}
                    row = owned(db, AgentSkill, UUID(identifier), checked)
                    if not row.enabled:
                        return {"error": "Skill 已停用"}
                    files = skill_files.bundle_files(row)
                    if name == "load_skill":
                        post = frontmatter.loads(row.markdown)
                        return {
                            "skill_id": identifier, "name": row.name,
                            "instructions": row.markdown,
                            "metadata": {key: post[key] for key in (
                                "name", "description", "license", "compatibility", "metadata", "allowed-tools",
                            ) if key in post},
                            "files": skill_files.manifest(files),
                            "capabilities": {"read_text_files": True, "execute_scripts": False,
                                             "interpret_binary_assets": False},
                        }
                    path = skill_files.safe_path(arguments["path"])
                    if path not in files:
                        return {"error": "Skill 文件不存在，请使用 load_skill 返回的文件路径"}
                    data = files[path]
                    result = {**skill_files.manifest({path: data})[0], "skill_id": identifier}
                    text = skill_files.text_content(data)
                    if text is None:
                        return {**result, "content": None, "message": "这是二进制资源，可从 Skill 管理页下载；当前工具不解析或执行该文件"}
                    start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
                    lines = text.splitlines(keepends=True)
                    offset = arguments.get("start_character", 0)
                    if start <= len(lines) and offset > len(lines[start - 1]):
                        return {**result, "error": "start_character 超出该行长度"}
                    selected = "".join(lines[start - 1:start - 1 + count])[offset:offset + 32000]
                    # Return an exact cursor so very long lines can also be read without data loss.
                    next_line, next_character = start, offset + len(selected)
                    while next_line <= len(lines) and next_character >= len(lines[next_line - 1]):
                        next_character -= len(lines[next_line - 1])
                        next_line += 1
                    return {**result, "content": selected, "start_line": start,
                            "start_character": offset, "total_lines": len(lines),
                            "next_line": next_line if next_line <= len(lines) else None,
                            "next_character": next_character if next_line <= len(lines) else None}
                if name not in self.bindings:
                    return {"error": "该工具未授权或不可用"}
                identifier, snapshot, _ = self.bindings[name]
                row = owned(db, MCPServer, identifier, checked)
                if not row.enabled or snapshot["name"] not in row.allowed_tools or snapshot not in row.tools:
                    return {"error": "MCP 连接或工具已停用、撤销授权或发生变化"}
                mcp_client.validate_tool(snapshot)
                Draft202012Validator(snapshot["inputSchema"], registry=Registry()).validate(arguments)
                connection = (row.url, row.transport, row.encrypted_headers)

                def before_call():
                    if check_active:
                        check_active()
                    with self.factory() as current_db:
                        current_access = self.access.revalidate(current_db)
                        current = owned(current_db, MCPServer, identifier, current_access)
                        if (
                            not current.enabled or snapshot["name"] not in current.allowed_tools
                            or snapshot not in current.tools
                            or (current.url, current.transport, current.encrypted_headers) != connection
                        ):
                            raise mcp_client.MCPError("MCP 连接配置或授权已变化，本次工具未执行")

                return mcp_client.call(
                    row.url, row.transport, mcp_client.decrypt_headers(row.encrypted_headers),
                    snapshot, arguments, before_call,
                )
        except (ValueError, TypeError, ValidationError, Unresolvable):
            return {"error": "工具参数不符合定义，请修正后重试"}
        except mcp_client.MCPError as exc:
            return {"error": str(exc)}
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise
            return {"error": "扩展不存在或授权已撤销"}
