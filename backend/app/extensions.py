"""Request-pinned platform methods, private methods and authorized remote tools."""
import json
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, HTTPException
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from . import mcp_client
from .models import AgentSkill, MCPServer, RemoteToolExecution
from .personal_skills import router as skills_router, owned, package_review
from .personal_mcp import router as mcp_router
from .site_skills import SkillRuntime, LOAD_SKILL, READ_SKILL_RESOURCE
from .skill_files import text_content
from .workspace_files import tool_definitions, execute_tool

router = APIRouter()
router.include_router(skills_router)
router.include_router(mcp_router)


def defaults(db, access):
    skills = db.scalars(select(AgentSkill.id).where(AgentSkill.owner_id == access.user_id,
        AgentSkill.workspace_id == access.workspace_id, AgentSkill.enabled.is_(True))).all()
    servers = db.scalars(select(MCPServer).where(MCPServer.owner_id == access.user_id,
        MCPServer.workspace_id == access.workspace_id, MCPServer.enabled.is_(True))).all()
    return list(map(str, skills)), [str(row.id) for row in servers if row.allowed_tools and row.last_checked_at]


def validate_selection(db, access, skill_ids, server_ids):
    for model, identifiers in ((AgentSkill, skill_ids), (MCPServer, server_ids)):
        if len(identifiers) > 100:
            raise HTTPException(422, "选择的扩展过多")
        for identifier in identifiers:
            row = owned(db, model, UUID(str(identifier)), access)
            if not row.enabled or model is MCPServer and (not row.allowed_tools or not row.last_checked_at):
                raise HTTPException(409, "选中的扩展已停用或尚未授权")


class ExtensionRuntime(SkillRuntime):
    def __init__(self, factory, access, skill_ids=(), server_ids=(), message_id=None, request_id=None):
        super().__init__(factory, access)
        self.message_id, self.request_id = message_id, request_id
        self.personal_ids, self.bindings, self.calls = set(), {}, 0
        self.uncertain_calls = set()
        self.mcp_tokens = {}
        with factory() as db:
            access.revalidate(db)
            for row in db.scalars(select(AgentSkill).options(joinedload(AgentSkill.files)).where(
                    AgentSkill.owner_id == access.user_id, AgentSkill.workspace_id == access.workspace_id,
                    AgentSkill.id.in_([UUID(str(item)) for item in skill_ids]), AgentSkill.enabled.is_(True))).unique():
                files = {item.path: item.content for item in row.files}
                checked = package_review({"SKILL.md": row.markdown.encode("utf-8"), **files})
                if checked["has_limitations"] and not row.limitations_acknowledged:
                    continue
                identifier = str(row.id)
                self.personal_ids.add(identifier)
                readable = {item["path"] for item in checked["manifest"] if item["readable"]}
                self.snapshots[identifier] = {"id": identifier, "name": row.name, "description": row.description,
                    "version": row.version, "source": "personal", "instructions": row.markdown,
                    "limitations": checked["issues"],
                    "resources": {path: text_content(content) for path, content in files.items() if path in readable}}
            for row in db.scalars(select(MCPServer).where(MCPServer.owner_id == access.user_id,
                    MCPServer.workspace_id == access.workspace_id,
                    MCPServer.id.in_([UUID(str(item)) for item in server_ids]), MCPServer.enabled.is_(True))):
                if not row.last_checked_at:
                    continue
                for tool in row.tools:
                    if tool["name"] not in row.allowed_tools:
                        continue
                    alias = f"mcp_{row.id.hex}_{mcp_client.tool_digest({'name': tool['name']})[:20]}"
                    self.bindings[alias] = {"id": row.id, "tool": tool, "name": row.name,
                        "url": row.url, "transport": row.transport, "credentials": row.encrypted_headers,
                        "authorization": row.authorization_version}
                    self.mcp_tokens[alias] = row.authorization_version
        self.skills = [{key: value for key, value in row.items()
            if key in ("id", "name", "description", "version", "source")} for row in self.snapshots.values()]
        self.tools = ([LOAD_SKILL, *([READ_SKILL_RESOURCE] if any(row["resources"] for row in self.snapshots.values()) else []), *tool_definitions()]
            if self.skills else []) + [{"type": "function", "function": {"name": alias,
                "description": f"{item['name']} / {item['tool']['name']}: {item['tool'].get('description', '')}"[:4000],
                "parameters": item["tool"]["inputSchema"]}} for alias, item in self.bindings.items()]
        self.versions = sorted([(row["id"], row["version"]) for row in self.skills] + list(self.mcp_tokens.items()))

    def label(self, name, arguments=None):
        binding = self.bindings.get(name)
        return f"{binding['name']} · {binding['tool']['name']}" if binding else super().label(name, arguments)

    def execute(self, name, arguments, check_active=None):
        if check_active:
            check_active()
        if name in ("load_skill", "read_skill_resource") and isinstance(arguments, dict) and str(arguments.get("skill_id")) in self.personal_ids:
            identifier = str(arguments["skill_id"])
            with self.factory() as db:
                self.access.revalidate(db)
                row = db.get(AgentSkill, UUID(identifier), populate_existing=True)
                if row is None or row.owner_id != self.access.user_id or row.workspace_id != self.access.workspace_id or not row.enabled:
                    return {"error": "该方法不存在或已停用"}
            return self._personal_read(name, arguments)
        if name not in self.bindings:
            if name in {item["function"]["name"] for item in tool_definitions()}:
                with self.factory() as db:
                    self.access.revalidate(db)
                    return execute_tool(name, arguments, db, self.access, self.access.workspace_id,
                        conversation_id=self.message_id, request_id=self.request_id)
            return super().execute(name, arguments, check_active)
        item = self.bindings[name]
        signature = json.dumps([name, arguments], sort_keys=True, ensure_ascii=True)
        if signature in self.uncertain_calls:
            return {"error": "上一次操作结果尚未确认，请先到对应服务核对", "uncertain": True}
        self.calls += 1
        if self.calls > 20:
            return {"error": "本次远程工具调用已达上限，请继续发送消息后再试"}
        try:
            mcp_client.validate_tool(item["tool"])
            Draft202012Validator(item["tool"]["inputSchema"], registry=Registry()).validate(arguments)
            def before_call():
                if check_active:
                    check_active()
                with self.factory() as db:
                    self.access.revalidate(db)
                    row = owned(db, MCPServer, item["id"], self.access)
                    if (not row.enabled or row.authorization_version != item["authorization"]
                            or item["tool"]["name"] not in row.allowed_tools or item["tool"] not in row.tools
                            or (row.url, row.transport, row.encrypted_headers) != (item["url"], item["transport"], item["credentials"])):
                        raise mcp_client.MCPError("连接或授权已变化，本次操作未执行")
            before_call()
            result = self._remote_call(name, item, arguments, signature, before_call)
            return {**result, "_extension": {"tool": name, "version": item["authorization"]}}
        except (ValueError, TypeError, ValidationError, Unresolvable):
            return {"error": "工具参数不符合定义，请修正后重试"}
        except mcp_client.MCPError as exc:
            uncertain = "未自动重试" in str(exc)
            if uncertain:
                self.uncertain_calls.add(signature)
            return {"error": str(exc), "uncertain": uncertain}
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise
            return {"error": "扩展不存在或授权已撤销"}

    def _remote_call(self, name, item, arguments, signature, before_call):
        execution_id = None
        if self.message_id:
            # Persist intent before contacting the service. A lost response must never
            # turn a conversation retry into a second external write.
            from sqlalchemy.exc import IntegrityError
            with self.factory() as db:
                previous = db.scalar(select(RemoteToolExecution).where(
                    RemoteToolExecution.message_id == self.message_id, RemoteToolExecution.tool_name == name,
                    RemoteToolExecution.arguments_digest == sha256(signature.encode()).hexdigest()))
                if previous:
                    if previous.authorization_version != item["authorization"]:
                        raise mcp_client.MCPError("历史操作授权已变化，请先核对原操作")
                    if previous.encrypted_result:
                        return json.loads(mcp_client.decrypt_headers(previous.encrypted_result)["tool_result"])
                    raise mcp_client.MCPError("此前操作结果尚未确认；未自动重试，请核对远端执行状态")
                # Verify result encryption is ready before a side effect is possible.
                mcp_client.encrypt_headers({"tool_result": "{}"})
                row = RemoteToolExecution(message_id=self.message_id, workspace_id=self.access.workspace_id,
                    owner_id=self.access.user_id, request_id=self.request_id, tool_name=name,
                    authorization_version=item["authorization"], arguments_digest=sha256(signature.encode()).hexdigest(),
                    status="running")
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    raise mcp_client.MCPError("该操作正在处理；未自动重试，请核对远端执行状态") from None
                execution_id = row.id
        try:
            result = mcp_client.call(item["url"], item["transport"], mcp_client.decrypt_headers(item["credentials"]),
                item["tool"], arguments, before_call)
        except BaseException as exc:
            if execution_id:
                with self.factory() as db:
                    db.get(RemoteToolExecution, execution_id).status = "uncertain"
                    db.commit()
                if isinstance(exc, mcp_client.MCPError) and "未自动重试" not in str(exc):
                    raise mcp_client.MCPError(f"{exc}；未自动重试，请核对远端执行状态") from None
            raise
        if execution_id:
            encrypted = mcp_client.encrypt_headers({"tool_result": json.dumps(result, ensure_ascii=True)})
            with self.factory() as db:
                row = db.get(RemoteToolExecution, execution_id)
                row.status, row.encrypted_result = ("failed" if result.get("isError") else "completed"), encrypted
                db.commit()
        return result

    def _personal_read(self, name, arguments):
        snapshot = self.snapshots[str(arguments["skill_id"])]
        if name == "load_skill":
            if set(arguments) != {"skill_id"}:
                return {"error": "工具参数无效"}
            self.loaded.add(snapshot["id"])
            return {key: snapshot[key] for key in ("id", "name", "version", "instructions", "limitations")} | {
                "resources": [{"path": path} for path in snapshot["resources"]], "execute_scripts": False}
        if (set(arguments) - {"skill_id", "path", "offset"} or snapshot["id"] not in self.loaded
                or not isinstance(arguments.get("path"), str)):
            return {"error": "请先加载 Skill，再读取清单内的文字资料"}
        content = snapshot["resources"].get(arguments["path"])
        offset = arguments.get("offset", 0)
        if content is None or type(offset) is not int or not 0 <= offset <= len(content):
            return {"error": "资料不存在、不能读取或读取位置无效"}
        end = min(offset + 12000, len(content))
        return {"id": snapshot["id"], "name": snapshot["name"], "version": snapshot["version"],
            "path": arguments["path"], "content": content[offset:end], "offset": offset,
            "total_characters": len(content), "next_offset": end if end < len(content) else None}
