"""Workspace file tools, application methods and authorized remote tools."""
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PayloadValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from sqlalchemy import select

from app.integrations import mcp_client
from app.models import MCPServer
from app.skills.personal import owned
from app.skills.catalog import LOAD_SKILL
from app.files.workspace import FILE_TOOL_ERRORS, tool_definitions, execute_tool
from app.files.image_reading import read_image
from app.infra.config import get_settings

def defaults(db, access):
    servers = db.scalars(select(MCPServer).where(MCPServer.owner_id == access.user_id,
        MCPServer.account_id == access.user_id, MCPServer.enabled.is_(True))).all()
    return [], [str(row.id) for row in servers if row.allowed_tools and row.last_checked_at]


def validate_selection(db, access, skill_ids, server_ids):
    for model, identifiers in ((MCPServer, server_ids),):
        if len(identifiers) > 100:
            raise HTTPException(422, "选择的扩展过多")
        for identifier in identifiers:
            row = owned(db, model, UUID(str(identifier)), access)
            if model is MCPServer and (not row.enabled or not row.allowed_tools or not row.last_checked_at):
                raise HTTPException(409, "选中的扩展已停用或尚未授权")


@dataclass(frozen=True)
class ImageToolResult:
    image: object
    result: dict


class ExtensionRuntime:
    def __init__(self, factory, access, skill_ids=(), server_ids=(), message_id=None, request_id=None,
                 question_workbench=False, provider_config=None):
        self.factory, self.access = factory, access
        self.skills = []
        self.message_id, self.request_id = message_id, request_id
        self.provider_config = provider_config
        self.bindings = {}
        self.verified_citations = []
        self.mcp_tokens = {}
        with factory() as db:
            access = self.access = access.revalidate(db)
            from app.skills.discovery import summary
            self.skills = summary(db, access)
            for row in db.scalars(select(MCPServer).where(MCPServer.owner_id == access.user_id,
                    MCPServer.account_id == access.user_id,
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
        from app.cloud_computer.runtime import Settings as CloudSettings
        with factory() as db:
            cloud_settings = db.get(CloudSettings, 1)
            self.execution_available = bool(cloud_settings and cloud_settings.enabled)
            from app.cloud_computer.environment import environment_description
            self.environment_description = environment_description(cloud_settings) if self.execution_available else ""
        definitions = tool_definitions()
        workspace_tools = [item for item in definitions
            if item["function"]["name"] == "get_workspace_file_link"
            or (self.execution_available and item["function"]["name"] != "read_image")
            or (item["function"]["name"] == "read_image" and provider_config
                and provider_config.supports_images)]
        from app.cloud_computer.preview import TOOLS as PREVIEW_TOOLS
        from app.cloud_computer.tools import TOOLS as COMPUTER_TOOLS
        if self.execution_available:
            workspace_tools += COMPUTER_TOOLS
        if self.execution_available and get_settings().preview_enabled:
            workspace_tools = workspace_tools + PREVIEW_TOOLS
        self.workspace_tool_names = {item["function"]["name"] for item in workspace_tools}
        from app.integrations.mcp_tool_catalog import Catalog, TOOLS as MCP_DISCOVERY_TOOLS
        self.tools = workspace_tools + (MCP_DISCOVERY_TOOLS if self.bindings else [])
        self.mcp_catalog = Catalog(self)
        self.versions = sorted(self.mcp_tokens.items())
        from app.images.tools import tools as image_tools
        with factory() as db:
            self.tools.extend(image_tools(db, access))
        from app.conversation_documents import TOOLS as DOCUMENT_TOOLS
        from app.knowledge_base_tools import TOOLS as KNOWLEDGE_TOOLS
        self.tools.extend(KNOWLEDGE_TOOLS)
        self.tools.extend(DOCUMENT_TOOLS)
        self.versions.append(("knowledge-base-tools", 1))
        self.versions.append(("conversation-documents", 1))
        self.versions.sort()
        from app.integrations.question_workbench import Runtime, TOOLS
        self.question_workbench = Runtime(factory, access, question_workbench, request_id, message_id)
        if self.question_workbench.version:
            if not any(item["function"]["name"] == "load_skill" for item in self.tools):
                self.tools.append(LOAD_SKILL)
            self.tools.extend(TOOLS)
            self.skills.append({"id": "question-workbench", "name": "教师出题", "description": "依据授权资料出题、保存到题目工作台并组卷导出", "version": 1, "source": "application"})
            self.versions.extend([(item["function"]["name"], self.question_workbench.version) for item in TOOLS])
            self.versions.append(("question-workbench", self.question_workbench.version))
            self.versions.sort()

    def _image_local_loader(self, path, check_active, call_id, computer_id, **execution):
        from app.cloud_computer.runtime import execute_image, tool_request_id
        from app.models import ChatMessage
        with self.factory() as db:
            access = self.access.revalidate(db)
            message = db.get(ChatMessage, self.message_id) if self.message_id else None
            image, receipt = execute_image(db, access, message.conversation_id if message else None,
                tool_request_id(self.request_id, call_id), path, check_active, computer_id=computer_id, **execution)
            from app.files.links import get_file_link, file_reference
            linked = get_file_link(db, access, {"path": path})
            return image.data, {"size_bytes": len(image.data), "sha256": image.image["sha256"],
                "files": [file_reference(linked)], "computer_id": receipt["computer_id"],
                "cloud_computer": receipt["cloud_computer"], "command_id": receipt["command_id"]}

    def model_tools(self, messages):
        return self.mcp_catalog.prepare(messages)

    def label(self, name, arguments=None):
        from app.cloud_computer.tools import LABELS as COMPUTER_LABELS
        if name in COMPUTER_LABELS:
            return COMPUTER_LABELS[name]
        if name in ("search_mcp_tools", "load_mcp_tool"):
            return "查找已授权工具" if name == "search_mcp_tools" else "加载已授权工具"
        from app.knowledge_base_tools import LABELS as KNOWLEDGE_LABELS
        if name in KNOWLEDGE_LABELS:
            return KNOWLEDGE_LABELS[name]
        from app.images.tools import LABELS as IMAGE_LABELS
        if name in IMAGE_LABELS:
            return IMAGE_LABELS[name]
        from app.conversation_documents import LABELS as DOCUMENT_LABELS
        if name in DOCUMENT_LABELS:
            return DOCUMENT_LABELS[name]
        from app.integrations.question_workbench import LABELS
        if name in LABELS:
            return LABELS[name]
        if name == "load_skill" and isinstance(arguments, dict) and arguments.get("skill_id") == "question-workbench":
            return "使用「教师出题」"
        binding = self.bindings.get(name)
        if name in self.workspace_tool_names:
            return {"get_workspace_file_link": "获取文件链接", "list_files": "查看工作区", "read_file": "读取文件", "read_image": "查看图片", "write_file": "生成文件",
                "apply_patch": "修改文件", "delete_file": "删除文件", "run_command": "云电脑执行",
                "start_preview": "启动成果预览", "stop_preview": "停止成果预览"}[name]
        return f"{binding['name']} · {binding['tool']['name']}" if binding else "读取任务方法"

    def execute(self, name, arguments, check_active=None, *, call_id=None,
                agent_tool_execution_id=None, on_progress=None):
        from app.tool_errors import public_error
        execution = {key: value for key, value in {
            "agent_tool_execution_id": agent_tool_execution_id, "on_progress": on_progress}.items()
            if value is not None}
        if check_active:
            check_active()
        from app.cloud_computer.tools import LABELS as COMPUTER_LABELS, execute as computer_tool
        if name in COMPUTER_LABELS:
            if name not in self.workspace_tool_names:
                return {"error": "云电脑尚未开放", "error_code": "cloud_computer_unavailable"}
            try:
                return computer_tool(self, name, arguments, check_active, call_id=call_id, **execution)
            except PayloadValidationError:
                return {"error": "请检查云电脑名称、ID 或分页参数", "error_code": "cloud_arguments"}
            except HTTPException as error:
                return {"error": public_error(error.detail), "error_code": "cloud_computer_unavailable",
                    "status_code": error.status_code}
        from app.integrations.mcp_tool_catalog import NAMES as MCP_DISCOVERY_NAMES
        if name in MCP_DISCOVERY_NAMES:
            return self.mcp_catalog.execute(name, arguments)
        from app.knowledge_base_tools import LABELS as KNOWLEDGE_LABELS, execute as knowledge_tool
        if name in KNOWLEDGE_LABELS:
            try:
                return knowledge_tool(self, name, arguments, call_id=call_id)
            except PayloadValidationError:
                return {"error": "参数不完整或无效，请确认知识库、名称及需要修改的字段", "error_code": "knowledge_arguments"}
            except HTTPException as error:
                detail = ("知识库或文档不存在或无权访问，请重新选择可访问的对象"
                    if error.status_code == 404 else public_error(error.detail))
                return {"error": detail, "error_code": "knowledge_access", "status_code": error.status_code}
            except Exception:
                return {"error": "操作结果尚未确认，请先查看知识库核对结果，再重试原调用", "uncertain": True,
                        "error_code": "knowledge_result"}
        from app.cloud_computer.preview import LABELS as PREVIEW_LABELS, execute as preview_tool
        if name in PREVIEW_LABELS:
            if name not in self.workspace_tool_names:
                return {"error": "成果预览尚未开放"}
            try:
                return preview_tool(self, name, arguments, check_active, call_id=call_id, **execution)
            except PayloadValidationError:
                return {"error": "请检查启动命令、网页端口和预览名称", "error_code": "preview_arguments"}
            except HTTPException as error:
                return {"error": public_error(error.detail), "error_code": "preview_unavailable", "status_code": error.status_code}
        from app.images.tools import LABELS as IMAGE_LABELS, execute as image_tool
        if name in IMAGE_LABELS:
            return image_tool(self, name, arguments, check_active=check_active, **execution)
        from app.conversation_documents import LABELS as DOCUMENT_LABELS, execute as document_tool
        if name in DOCUMENT_LABELS:
            try:
                return document_tool(self, name, arguments)
            except PayloadValidationError:
                return {"error": "保存参数不完整，请确认标题、内容和目标知识库", "error_code": "document_arguments"}
            except HTTPException as error:
                return {"error": public_error(error.detail), "error_code": "document_save", "status_code": error.status_code}
            except Exception:
                return {"error": "保存结果尚未确认，请查询已有任务后再试", "uncertain": True, "error_code": "document_save"}
        from app.integrations.question_workbench import LABELS, METHOD
        if name in LABELS or name == "load_skill" and isinstance(arguments, dict) and arguments.get("skill_id") == "question-workbench":
            try:
                with self.factory() as db:
                    self.question_workbench.check(db)
                if name == "load_skill":
                    return {"name": "教师出题", "instructions": METHOD, "version": 1}
                result = self.question_workbench.execute(name, arguments, check_active, call_id=call_id,
                    agent_tool_execution_id=agent_tool_execution_id)
                return {**result, "_extension": {"tool": name, "version": self.question_workbench.version}}
            except HTTPException as error:
                return {"error": public_error(error.detail) if error.status_code in (403, 404) else "题目工作台操作未完成"}
        if name not in self.bindings:
            if name in {item["function"]["name"] for item in tool_definitions()}:
                if name == "read_image" and not (
                        self.provider_config and self.provider_config.supports_images):
                    return {"error": "当前模型不支持图片输入，请选择支持图片的模型",
                            "error_code": "image_model_unsupported"}
                if name not in self.workspace_tool_names:
                    return {"error": "当前请求没有提供该文件工具", "error_code": "file_tool_unavailable"}
                definition = next(item["function"] for item in self.tools if item["function"]["name"] == name)
                try:
                    Draft202012Validator(definition["parameters"]).validate(arguments)
                except ValidationError:
                    return {"error": "工具参数不符合定义，请修正后重试", "error_code": "file_arguments"}
                if name == "read_image":
                    result = read_image({"path": arguments["path"]}, config=self.provider_config,
                        local_loader=lambda path: self._image_local_loader(path, check_active, call_id,
                            arguments["computer_id"], **execution),
                        check_active=check_active)
                    if hasattr(result, "public"):
                        return ImageToolResult(result, {**result.public,
                            "summary": f"已读取图片「{result.public['filename']}」"})
                    return result
                with self.factory() as db:
                    access = self.access.revalidate(db)
                    from app.models import ChatMessage
                    message = db.get(ChatMessage, self.message_id) if self.message_id else None
                    try:
                        from app.cloud_computer.runtime import tool_request_id
                        return execute_tool(name, arguments, db, access, access.user_id,
                            conversation_id=message.conversation_id if message else None,
                            request_id=tool_request_id(self.request_id, call_id) if name != "get_workspace_file_link" else None,
                            check_active=check_active, **execution)
                    except PayloadValidationError:
                        return {"error": "文件参数无效，请修正后重试", "error_code": "file_arguments"}
                    except HTTPException as error:
                        if error.status_code in (401, 403):
                            raise
                        if (error.status_code in (402, 503) and name != "get_workspace_file_link") or name == "run_command":
                            return {"error": public_error(error.detail), "error_code": "cloud_computer_unavailable",
                                    "status_code": error.status_code}
                        code = {404: "file_missing", 409: "file_conflict", 413: "file_capacity",
                            422: "file_arguments"}.get(error.status_code, "file_storage")
                        return {"error": public_error(error.detail, FILE_TOOL_ERRORS[code]), "error_code": code,
                                "status_code": error.status_code}
            return {"error": "该能力不可用"}
        item = self.bindings[name]
        if name not in self.mcp_catalog.offered or name not in self.mcp_catalog.loaded:
            return {"error": "工具尚未加载或已被移出当前模型预算；请先 load_mcp_tool，在下一模型步骤调用",
                "error_code": "mcp_tool_not_loaded"}
        self.mcp_catalog.loaded.move_to_end(name)
        signature = json.dumps([name, arguments], sort_keys=True, ensure_ascii=True)
        from app.integrations.receipts import ReceiptError
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
            result = self._remote_call(name, item, arguments, signature, before_call,
                call_id=call_id, agent_tool_execution_id=agent_tool_execution_id)
            return {**result, "_extension": {"tool": name, "version": item["authorization"],
                "server_id": str(item["id"])}}
        except ReceiptError as exc:
            return {"error": str(exc), "uncertain": exc.uncertain, "error_code": "tool_receipt"}
        except (ValueError, TypeError, ValidationError, Unresolvable):
            return {"error": "工具参数不符合定义，请修正后重试", "error_code": "tool_arguments"}
        except mcp_client.MCPError as exc:
            uncertain = "未自动重试" in str(exc)
            return {"error": public_error(exc), "uncertain": uncertain}
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise
            return {"error": "扩展不存在或授权已撤销"}

    def _remote_call(self, name, item, arguments, signature, before_call, *, call_id, agent_tool_execution_id):
        from app.integrations import receipts
        identifier = receipts.invocation_id(self.request_id, call_id, agent_tool_execution_id)
        annotations = item["tool"].get("annotations") or {}
        readonly = annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True
        execution_id, previous = receipts.claim(self.factory, self.access, self.message_id, self.request_id,
            identifier, name, signature, item["authorization"], readonly=readonly)
        if previous is not None:
            return previous
        try:
            result = mcp_client.call(item["url"], item["transport"], mcp_client.decrypt_headers(item["credentials"]),
                item["tool"], arguments, before_call)
        except BaseException as exc:
            receipts.finish(self.factory, execution_id, status="uncertain")
            if isinstance(exc, mcp_client.MCPError) and "未自动重试" not in str(exc):
                raise mcp_client.MCPError(f"{exc}；未自动重试，请核对远端执行状态") from None
            raise
        try:
            receipts.finish(self.factory, execution_id, result)
        except Exception:
            raise receipts.ReceiptError("操作已返回，但结果未能保存；请先核对原操作，不要重复执行", uncertain=True) from None
        return result
