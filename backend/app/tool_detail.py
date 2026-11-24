"""Read-only, tool-specific presentation of retained receipts."""
import json
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from .context_windows import decrypt_items
from app.integrations.mcp_client import decrypt_headers
from .models import AgentToolExecution, KnowledgeBaseStatus, WorkspaceFile, WorkspaceFileVersion
from .tool_result_reads import authorize, metadata

PAGE_SIZE = 4000
CAPTURE_LIMIT = 64_000
SECRET = re.compile(r"""(?ix)
    (?:https?|wss?)://[^\s<>'"]+
    | \b(?:Bearer|Basic)\s+[^\s,;]+
    | \b(?:authorization|cookie|set-cookie)\s*:\s*[^\r\n]+
    | \b(?:api[_-]?key|access[_-]?key|secret|password|token|authorization|cookie)
       [\s"']*[:=][\s"']*(?:(?:Bearer|Basic)\s+)?[^\s,;"']+
    | (?:-----BEGIN\ [^\n]*PRIVATE\ KEY-----).*?(?:-----END\ [^\n]*PRIVATE\ KEY-----|$)
    | \b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b
""", re.DOTALL)


def receipt_projection(db, access, conversation_id, request_id, execution_id, offset):
    row = db.get(AgentToolExecution, execution_id)
    if row is None or row.request_id != request_id or row.conversation_id != conversation_id:
        raise HTTPException(404, "操作详情不存在")
    if not row.encrypted_result:
        return {"text": "", "missing": True, "truncated": False, "redacted": False, "next_offset": None}
    message = decrypt_items(row.encrypted_result)[0]["message"]
    data = metadata(message.get("content"))
    authorize(db, access, row, message, data)
    args = json.loads(decrypt_headers(row.encrypted_arguments)["arguments"]) if row.encrypted_arguments else {}
    fields = []
    omitted = False

    def add(label, value):
        if isinstance(value, (str, int, float)) and value != "":
            fields.append(f"{label}：{value}")

    name = row.name
    if name == "run_command":
        add("命令", args.get("command"))
        add("退出状态", data.get("exit_code"))
        add("输出", data.get("stdout"))
        add("错误输出", data.get("stderr"))
        omitted = any(isinstance(item, dict) and item.get("truncated")
                      for item in (data.get("output_capture") or {}).values())
    elif name == "search_knowledge_base":
        for identifier in data.get("knowledge_base_ids", []):
            library = access.library(db, UUID(identifier))
            if library.status == KnowledgeBaseStatus.ARCHIVED:
                raise HTTPException(403, "知识库当前不可用")
            add("检索范围", library.name)
        add("检索词", data.get("query"))
        add("命中", data.get("result_count"))
        for item in data.get("evidence", []):
            add("来源", f"{item['knowledge_base_name']} · {item['filename']}")
            add("原文", item.get("text"))
    elif name in ("web_search", "web_fetch"):
        add("检索词", args.get("query"))
        add("命中", data.get("result_count"))
        for item in data.get("results", [data]):
            add("来源", item.get("title"))
            add("摘要", item.get("summary"))
        add("内容", data.get("content"))
    elif name in ("write_file", "apply_patch", "read_file", "read_image", "get_workspace_file_link"):
        add("操作", data.get("status"))
    elif data.get("task_id") and "files" in data:
        for key, label in (("size", "规格"), ("count", "请求数量"), ("n", "请求数量"), ("prompt", "图片描述")):
            add(label, args.get(key))
        add("图片任务", data.get("summary"))
    elif name.startswith("mcp_"):
        # Display standard MCP text blocks. Opaque objects and embedded resources
        # can include internal fields, transport metadata, and signed URIs.
        for item in data.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                add("错误" if data.get("isError") else "结果", item.get("text"))
            else:
                omitted = True
        omitted = omitted or bool(data.get("structuredContent") or data.get("_meta"))
    for file in (data.get("files") or data.get("generated_files") or ([data] if data.get("id") and data.get("path") else [])):
        try:
            resource = db.get(WorkspaceFile, UUID(str(file.get("id"))), populate_existing=True)
        except (TypeError, ValueError, AttributeError):
            continue
        if resource and resource.account_id == access.user_id and resource.deleted_at is None:
            version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == resource.id,
                WorkspaceFileVersion.version == file.get("version", 1)).execution_options(populate_existing=True))
            if version and version.account_id == access.user_id and version.deleted_at is None and version.available is not False:
                add("文件", resource.path)
    if data.get("error") or data.get("isError"):
        add("结果", "操作未完成")
    content, substitutions = SECRET.subn("[已隐藏]", "\n\n".join(fields))
    bounded = content[:CAPTURE_LIMIT]
    end = min(len(bounded), offset + PAGE_SIZE)
    return {
        "text": bounded[offset:end], "missing": not bool(bounded), "redacted": bool(substitutions) or omitted,
        "truncated": len(content) > len(bounded) or omitted or end < len(bounded),
        "next_offset": end if end < len(bounded) else None,
    }
