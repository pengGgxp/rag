"""Authorized reads from retained observations; no remote IO or billing."""
import json
from uuid import UUID

from fastapi import HTTPException

from .agent_tools import evidence_page, text_fields
from .models import AgentToolExecution, Chunk, Document, DocumentStatus, KnowledgeBaseStatus, MCPServer


def metadata(content):
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def authorize(db, access, row, message, data):
    access = access.revalidate(db)
    from .context_windows import decrypt_items
    seen = {row.id}
    # A page saved as another tool result still inherits the original access checks.
    while row.name == "read_tool_result" and data.get("_origin_result_id"):
        try:
            identifier = UUID(data["_origin_result_id"])
        except (ValueError, TypeError):
            raise HTTPException(403, "历史结果的来源无法核对")
        parent = db.get(AgentToolExecution, identifier)
        if identifier in seen or not parent or parent.conversation_id != row.conversation_id or not parent.encrypted_result:
            raise HTTPException(403, "历史结果的来源无法核对")
        seen.add(identifier)
        row = parent
        message = decrypt_items(row.encrypted_result)[0]["message"]
        data = metadata(message.get("content"))
    extension = data.get("_extension") or {}
    if row.name.startswith("mcp_"):
        try:
            server_id = UUID(extension.get("server_id") or row.name.split("_")[1])
        except (ValueError, IndexError, TypeError):
            raise HTTPException(403, "历史结果缺少可核对的授权信息")
        server = db.get(MCPServer, server_id, populate_existing=True)
        if (not server or server.owner_id != access.user_id or server.account_id != access.user_id
                or not server.enabled or extension.get("version") != server.authorization_version):
            raise HTTPException(403, "连接或授权已变化，不能读取此结果")
        from .mcp_client import tool_digest
        names = {f"mcp_{server.id.hex}_{tool_digest({'name': name})[:20]}" for name in server.allowed_tools}
        if row.name not in names:
            raise HTTPException(403, "该工具的授权已撤销")
    from .question_workbench import LABELS
    if row.name in LABELS:
        from .question_workbench import require_access
        from .models import QuestionWorkbenchGrant
        require_access(db, access)
        grant = db.get(QuestionWorkbenchGrant, (access.user_id, access.user_id), populate_existing=True)
        if not grant or not grant.enabled or grant.version != extension.get("version"):
            raise HTTPException(403, "题目工作台授权已变化，不能读取此结果")
    # Reading serialized JSON is also reading its embedded document text.
    knowledge = row.name == "search_knowledge_base"
    for evidence in data.get("evidence", []) if knowledge else []:
        try:
            chunk = db.get(Chunk, UUID(evidence["chunk_id"]), populate_existing=True)
            document = db.get(Document, UUID(evidence["version_id"]), populate_existing=True)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(403, "历史候选缺少可核对的文档版本")
        if (not chunk or not document or chunk.document_id != document.id or not document.is_active
                or document.status != DocumentStatus.READY
                or str(document.knowledge_base_id) != evidence.get("knowledge_base_id")):
            raise HTTPException(403, "文档已删除、更新或不可检索，请重新查询当前资料")
        if access.library(db, document.knowledge_base_id).status == KnowledgeBaseStatus.ARCHIVED:
            raise HTTPException(403, "知识库当前不可用")
    return knowledge


def read(data, content, identifier, arguments, budget, *, knowledge=False):
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 4000)
    if "candidate_offset" in arguments:
        if (not knowledge or "field" in arguments or "find" in arguments or "offset" in arguments
                or not isinstance(data.get("evidence"), list)):
            return {"error": "候选分页仅适用于知识库结果，不能与文本分页参数混用"}
        return {**evidence_page(data, identifier, budget, arguments["candidate_offset"]),
            "_origin_result_id": str(identifier)}
    fields = text_fields(data)
    field = arguments.get("field")
    if field is not None:
        if field not in fields:
            return {"error": "请选择 text_fields 中的文本区域", "text_fields": list(fields)[:40]}
        content = fields[field]
    total = len(content)
    offset = min(offset, total)
    if arguments.get("find") is not None:
        found = content.find(arguments["find"], offset)
        if found < 0:
            return {"result_id": str(identifier), "_origin_result_id": str(identifier), "field": field, "found": False, "offset": offset,
                "next_offset": None, "total_characters": total}
        offset = found
    limit = min(limit, max(1, (budget - 400) // 3))
    end = min(total, offset + limit)
    result = {"result_id": str(identifier), "_origin_result_id": str(identifier), "field": field, "offset": offset, "end_offset": end,
        "text": content[offset:end], "next_offset": end if end < total else None,
        "total_characters": total, "truncated": end < total, "text_fields": list(fields)[:40],
        "notice": "历史工具观察，未执行原操作；不代表当前资料状态或当前证据"}
    if "find" in arguments:
        result["found"] = True
    if knowledge and field and field.startswith("evidence.") and end > offset:
        index = int(field.split(".")[1])
        evidence = {**data["evidence"][index], "text": content[offset:end]}
        result["evidence"] = [evidence]
        result["notice"] = "已重新核对本页文档版本与权限；仅本页返回的正文可作当前证据"
    for key in ("source_id", "url", "title", "fetched_at", "exit_code", "status", "error", "uncertain", "isError", "output_capture"):
        if key in data:
            result[key] = data[key]
    return result
