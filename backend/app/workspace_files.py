import base64, hashlib, json
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from .auth import Access, get_access, now, aware
from .db import get_db
from .models import WorkspaceFile, WorkspaceFileVersion, WorkspaceExecutionJob
from .storage import download_fileobj, upload_fileobj
from .config import get_settings

router = APIRouter(prefix="/files", tags=["user-files"])
MAX_BYTES = 200 * 1024 * 1024
MAX_OUTPUT = 12000
FILE_TOOL_ERRORS = {
    "file_tool_unavailable": "当前无法生成或修改文件，请重新开始对话后再试",
    "file_arguments": "文件内容或路径不符合要求，正在等待修正",
    "skill_unavailable": "所选方法已停用或不可用，请重新选择后再试",
    "file_missing": "资源不存在或已删除，请重新选择",
    "file_conflict": "文件已变化，请重新读取后再修改",
    "file_capacity": "文件过大或存储空间不足，请缩小文件或增加额度",
    "file_download_quota": "下载额度不足，请增加额度后重试",
    "file_storage": "文件暂时无法保存或读取，请稍后重试；持续失败请联系管理员",
    "script_failed": "脚本未能完成，请根据错误调整后再试",
    "script_output_save": "脚本已运行，但部分文件未保存，请先查看已有输出，勿重复运行",
}
def valid_path(path: str) -> str:
    if not path or len(path) > 512 or path.startswith(("/", "\\")) or any(c in path for c in '\\:<>"|?*') or any(ord(c) < 32 for c in path):
        raise HTTPException(422, "文件路径无效")
    p = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in path.split("/")): raise HTTPException(422, "文件路径无效")
    if any(part.endswith((" ", ".")) or part == ".rag-directory" for part in path.split("/")):
        raise HTTPException(422, "名称不能以空格或句点结尾，也不能使用保留名称")
    return str(p)
def check_workspace(access, account_id, db):
    if UUID(str(account_id)) != access.user_id:
        raise HTTPException(404, "资源不存在或无权访问")
def read_file(row):
    from .file_operations import file_revision
    return {"id": str(row.id), "path": row.path, "content_type": row.content_type, "size_bytes": row.size_bytes, "sha256": row.content_hash, "version": 1, "revision": file_revision(row), "availability": "missing" if row.deleted_at else "available", "source": row.source, "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
        "download_url": f"/resources/{row.id}/download", "preview_url": f"/resources/{row.id}/download?preview=true"}
def owned(db, access, account_id, file_id, write=False):
    check_workspace(access, account_id, db)
    row = db.get(WorkspaceFile, file_id)
    if not row or row.deleted_at or row.owner_user_id != access.user_id:
        raise HTTPException(404, "文件不存在")
    return row
class TextWrite(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=5_000_000)
    expected_version: int | None = Field(default=None, ge=1)
    revision: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    content_type: str = "text/plain"
class PatchWrite(BaseModel):
    patch: str = Field(min_length=1, max_length=500_000)
    expected_version: int = Field(ge=1)


def _save_bytes(account_id, access, db, path, data, content_type="application/octet-stream", expected_version=None, *, expected_revision=None):
    """Internal writer; interactive clients use revision-mandatory write_text."""
    check_workspace(access, account_id, db)
    from .workspace_mutations import save_files
    from .file_operations import file_revision
    from .shared_resources import refresh_current_metadata
    row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id, WorkspaceFile.path == path))
    if row and row.deleted_at:
        raise HTTPException(409, "该路径的文件已删除，请重新创建")
    if row:
        row = refresh_current_metadata(db, access, row)
    expected = expected_revision if expected_revision is not None else file_revision(row) if row else None
    return save_files(db, access, {path: (data, content_type)}, expected={path: expected} if expected else {})["items"][0]


def _current_file_version(db, row):
    version = db.scalar(select(WorkspaceFileVersion).where(
        WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == row.current_version))
    if version is None:
        raise HTTPException(409, "文件保存未完成，请重新生成该文件")
    if version.deleted_at or version.available is False:
        raise HTTPException(404, "资源文件已不可用")
    return version


def _stage_bytes(account_id, access, db, path, data, content_type, expected_version, uploaded=None):
    path = valid_path(path)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "文件不能超过 200 MiB")
    from .storage_billing import check_capacity
    from .shared_resources import object_key
    check_capacity(db, access.user_id, len(data), exclude_key=object_key(access.user_id, path))
    row = db.scalar(select(WorkspaceFile).where(
        WorkspaceFile.owner_user_id == access.user_id, WorkspaceFile.path == path).execution_options(populate_existing=True))
    active = row is not None and row.deleted_at is None
    if row is not None and row.deleted_at:
        raise HTTPException(409, "该路径的资源已删除，请使用新路径保存")
    if active and expected_version not in (None, 1):
        raise HTTPException(409, "文件已被修改，请重新读取")
    if row is None:
        row = WorkspaceFile(account_id=access.user_id, path=path,
            content_type=content_type, size_bytes=0, content_hash="", current_version=1,
            created_by=access.user_id, updated_by=access.user_id)
        db.add(row)
        db.flush()
    digest = hashlib.sha256(data).hexdigest()
    row.content_type = content_type or row.content_type or "application/octet-stream"
    if row.content_type == "application/octet-stream":
        import mimetypes
        row.content_type = mimetypes.guess_type(path)[0] or row.content_type
    row.size_bytes, row.content_hash = len(data), digest
    row.current_version, row.updated_by, row.deleted_at = 1, access.user_id, None
    row.expires_at = datetime(9999, 12, 31, tzinfo=timezone.utc)
    from .shared_resources import object_key
    key = object_key(access.user_id, path)
    from .shared_resources import backend
    mounted = backend(db)
    if active:
        raise HTTPException(409, "输出路径已有文件，请先在工作区移走该文件后重试")
    if mounted is not None and getattr(mounted, "conditional_create", False):
        mounted.put(key, __import__('io').BytesIO(data), row.content_type, create=True)
    else:
        upload_fileobj(__import__('io').BytesIO(data), key, row.content_type)
    row.live_etag = (mounted.head(key) or {}).get("ETag") if mounted else None
    if uploaded is not None and not active: uploaded.append(key)
    # The mounted directory is the source of truth. Keep exactly one current
    # metadata row and remove legacy historical pointers when a file is replaced.
    db.query(WorkspaceFileVersion).filter(WorkspaceFileVersion.file_id == row.id,
        WorkspaceFileVersion.version != 1).delete(synchronize_session=False)
    version_row = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
        WorkspaceFileVersion.version == 1).execution_options(populate_existing=True))
    if version_row is None:
        db.add(WorkspaceFileVersion(file_id=row.id, account_id=access.user_id, version=1,
            object_key=key, content_type=row.content_type, size_bytes=len(data), content_hash=digest,
            created_by=access.user_id))
    else:
        version_row.storage_key, version_row.content_type = key, row.content_type
        version_row.size_bytes, version_row.content_hash = len(data), digest
        version_row.deleted_at, version_row.available = None, True
    db.flush()
    from .audit_log import record_audit
    record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
        action="resource.updated" if active else "resource.created", resource_type="resource",
        resource_id=row.id, metadata={"source": "upload"})
    return row
@router.get("")
def listing(q: str = "", offset: int = 0, limit: int = 100, db=Depends(get_db), access: Access=Depends(get_access)):
    limit=min(max(limit,1),500)
    rows=db.scalars(select(WorkspaceFile).where(
        WorkspaceFile.owner_user_id == access.user_id,
        WorkspaceFile.deleted_at.is_(None),
        WorkspaceFile.path.ilike(f"%{q}%"),
    ).order_by(WorkspaceFile.path).offset(max(offset,0)).limit(limit)).all()
    return {"items":[read_file(r) for r in rows],"offset":offset,"limit":limit}
@router.get("/{file_id}")
def detail(file_id: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    return read_file(owned(db, access, access.user_id, file_id))
def write_text(payload: TextWrite, db, access):
    from .workspace_mutations import save_files
    return save_files(db, access, {payload.path: (payload.content.encode("utf-8"), payload.content_type)},
        expected={payload.path: payload.revision} if payload.revision else {}, request_id=payload.request_id)["items"][0]


@router.put("")
def write_text_endpoint(payload: TextWrite, db=Depends(get_db), access: Access=Depends(get_access)):
    return write_text(payload, db, access)


@router.get("/{file_id}/text")
def read_text_endpoint(file_id: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    row = owned(db, access, access.user_id, file_id)
    from .shared_resources import refresh_current_metadata
    row = refresh_current_metadata(db, access, row)
    version = _current_file_version(db, row)
    from .file_operations import file_revision
    before_revision = file_revision(row)
    if not (row.content_type.startswith("text/") or row.path.lower().endswith((".md", ".txt", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".yaml", ".yml"))):
        raise HTTPException(415, "该文件不是可编辑文本")
    if version.size_bytes > 5_000_000:
        raise HTTPException(413, "文件过大，无法在浏览器中编辑")
    from .file_operations import storage as get_storage
    mounted = get_storage(db)
    try:
        with mounted.download(version.storage_key) as body:
            data = body.read()
        row = refresh_current_metadata(db, access, row)
        if before_revision != file_revision(row):
            raise HTTPException(409, "文件在读取时发生变化，请重新加载")
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "文件不是 UTF-8 文本") from None
    return {"path": row.path, "content": content, "version": row.current_version, "revision": file_revision(row), "content_type": row.content_type}
@router.api_route("/{file_id}/download", methods=["GET", "HEAD"])
def download(file_id: UUID,request: Request,db=Depends(get_db),access: Access=Depends(get_access)):
    from .storage_downloads import download_response
    row = owned(db,access,access.user_id,file_id)
    version = _current_file_version(db, row)
    return download_response(db, access, request, object_key=version.storage_key, size=version.size_bytes,
        filename=row.path.split("/")[-1], content_type=row.content_type, resource=f"output:{row.id}:{version.version}")
def _apply_unified_patch(original: str, patch: str) -> str:
    lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)
    out, cursor, i = [], 0, 0
    while i < len(patch_lines):
        line = patch_lines[i]
        if not line.startswith("@@"):
            i += 1
            continue
        import re
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not match:
            raise HTTPException(422, "补丁格式无效")
        start = int(match.group(1)) - 1
        if start < cursor or start > len(lines):
            raise HTTPException(409, "补丁目标已变化，请重新读取文件")
        out.extend(lines[cursor:start]); cursor = start; i += 1
        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            item = patch_lines[i]; i += 1
            if item.startswith("\\ No newline"):
                continue
            if not item:
                continue
            prefix, value = item[0], item[1:]
            if prefix == " ":
                if cursor >= len(lines) or lines[cursor] != value:
                    raise HTTPException(409, "补丁目标已变化，请重新读取文件")
                out.append(lines[cursor]); cursor += 1
            elif prefix == "-":
                if cursor >= len(lines) or lines[cursor] != value:
                    raise HTTPException(409, "补丁目标已变化，请重新读取文件")
                cursor += 1
            elif prefix == "+":
                out.append(value)
            else:
                raise HTTPException(422, "补丁格式无效")
    out.extend(lines[cursor:])
    return "".join(out)

def tool_definitions():
    definitions = [{"type":"function","function":{"name":n,"description":d,"parameters":p}} for n,d,p in [
        ("get_workspace_file_link","获取指定工作区文件的真实查看链接。只传 id 或 path；相对路径或 /home/user/workspace/ 下的绝对路径均可。不会启动云电脑、下载正文或挑选成果。命令生成文件后可用其明确路径获取链接，回答中用 [文件名](url) 交付。",{"type":"object","properties":{"id":{"type":"string"},"path":{"type":"string"}},"oneOf":[{"required":["id"]},{"required":["path"]}],"additionalProperties":False}),
        ("list_files","在指定云电脑列出工作区资源文件；工作区文件跨实例共享，依赖和临时文件属于各实例",{"type":"object","properties":{"query":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":100}},"additionalProperties":False}),
        ("read_file","在云电脑按资源 ID 或路径读取当前文件；二进制返回可直接用于脚本的路径，不代表已识别图片或解析文档正文。Word/PDF 用 run_command 调用对应解析器。",{"type":"object","properties":{"id":{"type":"string"},"path":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":12000}},"anyOf":[{"required":["path"]},{"required":["id"]}],"additionalProperties":False}),
        ("read_image","按需把工作区中的 PNG、JPEG 或静态 WebP 作为视觉输入读取并关联可打开文件。相对 path 以 /home/user/workspace 为基准；临时目录或远程图片先保存到工作区再读取。工具顺序由你判断。",{"type":"object","properties":{"path":{"type":"string","maxLength":1024}},"required":["path"],"additionalProperties":False}),
        ("write_file","创建或覆盖当前工作区文本文件，内容直接写入资源库。",{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"],"additionalProperties":False}),
        ("apply_patch","直接修改当前文件；patch 使用含 @@ -旧行号,行数 +新行号,行数 @@ 的 unified diff。嵌入图片请用 run_command 在脚本内读写，不要转抄 Base64。",{"type":"object","properties":{"path":{"type":"string"},"patch":{"type":"string"}},"required":["path","patch"],"additionalProperties":False}),
        ("delete_file","删除当前工作区文件",{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}),
        ("run_command","在明确指定的按存续时间计费云电脑执行 Python、Node 或 bash，不会创建或切换环境。工作目录 /home/user/workspace 是工作区资源库，文件直接操作并持久保存；同工作区不同实例共享文件，同一实例保留依赖。预装软件见环境能力说明，缺少依赖可用 pip/npm 安装到工作目录之外。编辑前检查输入，用对应库处理 Word/PDF；完成后重新打开输出核对内容。Skill 就是资源库普通文件，可用 list_files 查找 SKILL.md，read_file 读取，按其所在目录运行脚本；修改和删除使用普通文件工具。图片嵌入应在脚本内读写，不打印 Base64。不要求先列文件或加载 Skill。网页服务用 start_preview 启动，不在此等待服务退出。",{"type":"object","properties":{"command":{"type":"string"}},"required":["command"],"additionalProperties":False}),
    ]]
    for tool in definitions:
        function = tool["function"]
        if function["name"] == "list_files":
            function["parameters"]["properties"]["knowledge"] = {
                "type": "boolean", "description": "列出云电脑只读挂载的知识库原件，可按原文件名筛选"}
        if function["name"] == "read_file":
            function["description"] += "知识库原件使用 /home/user/knowledge 下的绝对路径。"
        if function["name"] == "get_workspace_file_link":
            continue
        function["parameters"]["properties"]["computer_id"] = {"type": "string", "format": "uuid",
            "description": "明确指定已创建的云电脑 ID；本工具不会自动创建或切换"}
        function["parameters"].setdefault("required", []).append("computer_id")
        function["description"] = function["description"].replace("当前会话云电脑", "指定云电脑")
    return definitions


def resolve_input(db, access, reference):
    try:
        identifier = UUID(reference["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "资源 ID 无效") from None
    row = owned(db, access, access.user_id, identifier)
    from .shared_resources import refresh_current_metadata
    row = refresh_current_metadata(db, access, row)
    return row, _current_file_version(db, row)

def execute_tool(name, arguments, db, access, account_id, conversation_id=None, request_id=None, check_active=None,
                 **execution):
    check_workspace(access, account_id, db)
    if name == "get_workspace_file_link":
        from .workspace_file_links import get_file_link
        return get_file_link(db, access, arguments)
    from .cloud_computer import execute
    if name == "run_command":
        return execute(db, access, conversation_id, request_id, arguments, check_active, **execution)
    if name not in {"list_files", "read_file", "write_file", "apply_patch", "delete_file"}:
        return {"error": "未知文件工具"}
    from .knowledge_originals import MOUNT_ROOT
    path = arguments.get("path") or ""
    if (name == "list_files" and arguments.get("knowledge")) or (
            name == "read_file" and path.startswith(MOUNT_ROOT + "/") and not arguments.get("id")):
        return execute_knowledge_read(name, arguments, db, access, conversation_id, request_id, check_active, **execution)
    from .shared_resources import refresh_index
    if name == "list_files" or (arguments.get("path") and not arguments.get("id")):
        refresh_index(db, access, arguments.get("path", arguments.get("query", "")), 0, 100)
    operation = {"name": name}
    metadata = {}
    row = None
    if name == "list_files":
        operation.update(query=arguments.get("query", ""), offset=max(0, arguments.get("offset", 0)),
                         limit=min(max(1, arguments.get("limit", 100)), 100))
    if name=="read_file":
        reference = arguments
        if not arguments.get("id"):
            row = db.scalar(select(WorkspaceFile).where(
                WorkspaceFile.owner_user_id == access.user_id,
                WorkspaceFile.path == valid_path(arguments.get("path", "")),
                WorkspaceFile.deleted_at.is_(None),
            ))
            if row is None: raise HTTPException(404, "资源不存在")
            reference = {**arguments, "id": str(row.id)}
        row, ver = resolve_input(db, access, reference)
        metadata = {**read_file(row), "version": 1, "size_bytes": ver.size_bytes,
            "content_type": ver.content_type, "sha256": ver.content_hash,
            "download_url": f"/resources/{row.id}/download",
            "preview_url": f"/resources/{row.id}/download?preview=true",
            "input": {"id": str(row.id), "version": 1}}
        operation.update(path=row.path, offset=max(0, arguments.get("offset", 0)),
            limit=min(max(1, arguments.get("limit", 12000)), 12000), binary=not (
                ver.content_type.startswith("text/") or row.path.lower().endswith(
                    (".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".css", ".sh"))))
        operation["reference"] = {"id": str(row.id), "version": 1}
        metadata["file_path"] = "/home/user/workspace/" + row.path
    elif name in {"write_file", "apply_patch", "delete_file"}:
        path = valid_path(arguments["path"])
        if name != "delete_file":
            from .storage_billing import check_capacity
            from .shared_resources import object_key
            check_capacity(db, access.user_id, len(arguments.get("content", "").encode("utf-8")),
                exclude_key=object_key(access.user_id, path))
        row = db.scalar(select(WorkspaceFile).where(
            WorkspaceFile.owner_user_id == access.user_id,
            WorkspaceFile.path == path,
            WorkspaceFile.deleted_at.is_(None),
        ))
        if name != "write_file" and row is None:
            raise HTTPException(404, "文件不存在")
        operation.update(path=path)
        if name == "write_file":
            payload = TextWrite(path=path, content=arguments.get("content", ""))
            operation["content"] = payload.content
        if name == "apply_patch":
            payload = PatchWrite(patch=arguments["patch"], expected_version=1)
            operation["patch"] = payload.patch
    result = execute(db, access, conversation_id, request_id, {
        "command": name, "computer_id": arguments.get("computer_id"), "file_operation": operation}, check_active, **execution)
    file_result = result.get("file_result", {})
    if file_result.get("error"):
        raise HTTPException(file_result["status_code"], file_result["error"])
    if result.get("status") != "completed":
        return result
    value = file_result.get("value", {})
    if name == "list_files":
        indexed = {r.path: r for r in db.scalars(select(WorkspaceFile).where(
            WorkspaceFile.owner_user_id == access.user_id,
            WorkspaceFile.deleted_at.is_(None))
            .execution_options(populate_existing=True))}
        value["items"] = [{**(read_file(indexed[item["path"]]) if item["path"] in indexed else {}), **item}
                          for item in value.get("items", [])]
    elif name in {"write_file", "apply_patch", "delete_file"}:
        from .shared_resources import register_operation
        value = register_operation(db, access, operation, value)
        if name in {"write_file", "apply_patch"}:
            metadata = value
    if name in {"read_file", "write_file", "apply_patch", "delete_file"}:
        from .workspace_file_links import file_reference
        if name == "delete_file":
            deleted = db.scalar(select(WorkspaceFile).where(
                WorkspaceFile.owner_user_id == access.user_id,
                WorkspaceFile.path == operation["path"]))
            if deleted:
                metadata = read_file(deleted)
        metadata = {**metadata, "url": f"/resources?file={metadata['id']}"} if metadata.get("id") else metadata
        if metadata.get("id"):
            value["files"] = [file_reference(metadata)]
    return {**metadata, **value, "computer_id": result["computer_id"],
        "cloud_computer": result["cloud_computer"], "command_id": result["command_id"]}


def execute_knowledge_read(name, arguments, db, access, conversation_id, request_id, check_active, **execution):
    from .cloud_computer import execute
    from .knowledge_originals import MOUNT_ROOT, mount_path, prefix
    from .models import Document, DocumentStatus, KnowledgeBase

    root = prefix(access.user_id)
    query = select(Document).join(KnowledgeBase).where(
        KnowledgeBase.account_id == access.user_id, Document.is_active.is_(True),
        Document.status != DocumentStatus.DELETED, Document.object_key.startswith(root, autoescape=True))
    offset = max(0, arguments.get("offset", 0))
    limit = arguments.get("limit")
    operation = {"name": name, "knowledge": True}
    if name == "list_files":
        limit = min(max(1, limit or 100), 100)
        if search := arguments.get("query"):
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where(Document.filename.ilike(pattern, escape="\\"))
        rows = db.scalars(query.order_by(Document.filename, Document.id).offset(offset).limit(limit + 1)).all()
        paths = {row.object_key[len(root):]: row for row in rows[:limit]}
        operation["paths"] = list(paths)
    else:
        relative = valid_path(arguments["path"][len(MOUNT_ROOT) + 1:])
        row = db.scalar(query.where(Document.object_key == root + relative))
        if row is None:
            raise HTTPException(404, "知识库原件不可用")
        operation.update(path=relative, offset=offset, limit=min(max(1, limit or 12000), 12000),
                         binary=not (row.content_type.startswith("text/") or relative.endswith((".md", ".txt", ".csv"))))
    result = execute(db, access, conversation_id, request_id, {
        "command": name, "computer_id": arguments.get("computer_id"), "file_operation": operation},
        check_active, **execution)
    file_result = result.pop("file_result", {})
    if file_result.get("error"):
        raise HTTPException(file_result["status_code"], file_result["error"])
    if result.get("status") != "completed":
        return result
    value = file_result.get("value", {})
    if name == "list_files":
        return {**result, "offset": offset, "limit": limit, "has_more": len(rows) > limit,
                "items": [{"path": f"{MOUNT_ROOT}/{item['path']}", "filename": paths[item["path"]].filename,
                           "document_id": str(paths[item["path"]].id), "size_bytes": item["size_bytes"]}
                          for item in value.get("items", []) if item["path"] in paths]}
    return {**result, **value, "file_path": mount_path(row, access.user_id), "filename": row.filename,
            "document_id": str(row.id)}
