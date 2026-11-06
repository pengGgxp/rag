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

router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["workspace-files"])
MAX_BYTES = 200 * 1024 * 1024
MAX_OUTPUT = 12000
FILE_TOOL_ERRORS = {
    "file_tool_unavailable": "当前无法生成或修改文件，请重新开始对话后再试",
    "file_arguments": "文件内容或路径不符合要求，正在等待修正",
    "skill_unavailable": "所选方法已停用或不可用，请重新选择后再试",
    "file_missing": "资源不存在或已删除，请重新选择",
    "file_conflict": "文件已变化，请重新读取后再修改",
    "file_capacity": "文件过大或工作区空间不足，请缩小文件或增加额度",
    "file_download_quota": "工作区下载额度不足，请联系工作区管理员增加额度后重试",
    "file_storage": "文件暂时无法保存或读取，请稍后重试；持续失败请联系管理员",
    "script_failed": "脚本未能完成，请根据错误调整后再试",
    "script_output_save": "脚本已运行，但部分文件未保存，请先查看已有输出，勿重复运行",
}
def valid_path(path: str) -> str:
    if not path or len(path) > 512 or path.startswith(("/", "\\")) or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        raise HTTPException(422, "文件路径无效")
    p = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in path.split("/")): raise HTTPException(422, "文件路径无效")
    return str(p)
def check_workspace(access, workspace_id):
    if UUID(str(workspace_id)) != access.workspace_id: raise HTTPException(404, "工作区不存在或无权访问")
def read_file(row):
    return {"id": str(row.id), "path": row.path, "content_type": row.content_type, "size_bytes": row.size_bytes, "sha256": row.content_hash, "version": row.current_version, "source": row.source, "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
        "download_url": f"/resources/{row.id}/download?version={row.current_version}", "preview_url": f"/resources/{row.id}/download?version={row.current_version}&preview=true"}
def owned(db, access, workspace_id, file_id, write=False):
    check_workspace(access, workspace_id); row = db.get(WorkspaceFile, file_id)
    if not row or row.workspace_id != access.workspace_id or row.deleted_at: raise HTTPException(404, "文件不存在")
    if write: access.editor()
    return row
class TextWrite(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=5_000_000)
    expected_version: int | None = Field(default=None, ge=1)
    content_type: str = "text/plain"
class PatchWrite(BaseModel):
    patch: str = Field(min_length=1, max_length=500_000)
    expected_version: int = Field(ge=1)


def _save_bytes(workspace_id, access, db, path, data, content_type="application/octet-stream", expected_version=None):
    uploaded = []
    try:
        with db.begin_nested():
            row = _stage_bytes(workspace_id, access, db, path, data, content_type, expected_version, uploaded)
            db.flush()
        db.commit()
    except BaseException:
        db.rollback()
        from .storage import delete_object
        for key in uploaded:
            try: delete_object(key)
            except Exception: pass
        raise
    return read_file(row)


def _current_file_version(db, row):
    version = db.scalar(select(WorkspaceFileVersion).where(
        WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == row.current_version))
    if version is None:
        raise HTTPException(409, "文件保存未完成，请重新生成该文件")
    if version.deleted_at or version.available is False:
        raise HTTPException(404, "资源文件已不可用")
    return version


def _stage_bytes(workspace_id, access, db, path, data, content_type, expected_version, uploaded=None):
    path = valid_path(path)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "文件不能超过 200 MiB")
    from .storage_billing import check_capacity
    check_capacity(db, access.workspace_id, len(data))
    row = db.scalar(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.path == path).execution_options(populate_existing=True))
    active = row is not None and row.deleted_at is None
    if active:
        if expected_version != row.current_version:
            raise HTTPException(409, "文件已被修改，请重新读取")
        version = row.current_version + 1
    else:
        if row is not None:
            raise HTTPException(409, "该路径的资源已删除，请使用新路径保存")
        version = 1
        if row is None:
            row = WorkspaceFile(workspace_id=access.workspace_id, path=path,
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
    row.current_version, row.updated_by, row.deleted_at = version, access.user_id, None
    row.expires_at = datetime(9999, 12, 31, tzinfo=timezone.utc)
    key = f"workspaces/{access.workspace_id}/files/{row.id}/{version}/{path}"
    upload_fileobj(__import__('io').BytesIO(data), key, row.content_type)
    if uploaded is not None: uploaded.append(key)
    db.add(WorkspaceFileVersion(file_id=row.id, workspace_id=access.workspace_id, version=version,
        object_key=key, content_type=row.content_type, size_bytes=len(data), content_hash=digest,
        created_by=access.user_id))
    from .shared_resources import publish
    db.flush()
    publish(db, row, _current_file_version(db, row))
    from .audit_log import record_audit
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action="resource.updated" if active else "resource.created", resource_type="resource",
        resource_id=row.id, metadata={"version": version})
    return row
@router.get("")
def listing(workspace_id: UUID, q: str = "", offset: int = 0, limit: int = 100, db=Depends(get_db), access: Access=Depends(get_access)):
    check_workspace(access, workspace_id); limit=min(max(limit,1),500); rows=db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id, WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.ilike(f"%{q}%")).order_by(WorkspaceFile.path).offset(max(offset,0)).limit(limit)).all(); return {"items":[read_file(r) for r in rows],"offset":offset,"limit":limit}
@router.get("/{file_id}")
def detail(workspace_id: UUID, file_id: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    return read_file(owned(db,access,workspace_id,file_id))
def write_text(workspace_id: UUID, payload: TextWrite, db, access):
    check_workspace(access, workspace_id); access.editor()
    return _save_bytes(workspace_id, access, db, payload.path, payload.content.encode("utf-8"),
        payload.content_type, payload.expected_version)
@router.api_route("/{file_id}/download", methods=["GET", "HEAD"])
def download(workspace_id: UUID,file_id: UUID,request: Request,db=Depends(get_db),access: Access=Depends(get_access)):
    from .storage_downloads import download_response
    row = owned(db,access,workspace_id,file_id)
    version = _current_file_version(db, row)
    return download_response(db, access, request, object_key=version.object_key, size=version.size_bytes,
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
    resource_input = {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"},
        "version": {"type": "integer", "minimum": 1}}, "required": ["id", "version"], "additionalProperties": False}
    return [{"type":"function","function":{"name":n,"description":d,"parameters":p}} for n,d,p in [
        ("list_files","列出当前工作区资源元信息，不下载正文",{"type":"object","properties":{"query":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":500}},"additionalProperties":False}),
        ("read_file","按资源 ID 或路径读取文字片段；二进制返回可用于命令输入的资源引用",{"type":"object","properties":{"id":{"type":"string"},"version":{"type":"integer","minimum":1},"path":{"type":"string"},"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":12000}},"anyOf":[{"required":["path"]},{"required":["id"]}],"additionalProperties":False}),
        ("write_file","创建或覆盖当前工作区文本文件",{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"expected_version":{"type":"integer"}},"required":["path","content"],"additionalProperties":False}),
        ("apply_patch","按版本应用文本文件补丁；patch 使用含 @@ -旧行号,行数 +新行号,行数 @@ 的 unified diff。嵌入图片请用 run_command 在脚本内读写，不要转抄 Base64。",{"type":"object","properties":{"path":{"type":"string"},"patch":{"type":"string"},"expected_version":{"type":"integer"}},"required":["path","patch","expected_version"],"additionalProperties":False}),
        ("delete_file","删除当前工作区文件",{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}),
        ("run_command","在按存续时间计费的云电脑执行 Python、Node 或 bash。工作目录 /home/user/workspace 直接挂载整个工作区资源库，同工作区所有会话共享当前文件，写入和删除直接生效。inputs 指定当前版本时无需传输；显式历史版本放在 /tmp/zhiku-cloud-computer/inputs/资源ID/版本/原路径，不覆盖当前文件。同一环境保留依赖。方法文件通过 skill_ids 或命令中的 /skills/方法ID/ 路径准备，位于 /skills；依赖写到工作目录之外。图片嵌入应在脚本内读写，不打印 Base64。不要求先列文件或加载 Skill。",{"type":"object","properties":{"command":{"type":"string","maxLength":2000},"inputs":{"type":"array","items":resource_input,"maxItems":50},"skill_ids":{"type":"array","items":{"type":"string"},"maxItems":20}},"required":["command"],"additionalProperties":False}),
    ]]


def resolve_input(db, access, reference):
    try:
        identifier = UUID(reference["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "资源 ID 无效") from None
    row = owned(db, access, access.workspace_id, identifier)
    version = db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id == row.id,
        WorkspaceFileVersion.version == reference.get("version", row.current_version),
        WorkspaceFileVersion.deleted_at.is_(None)))
    if version is None:
        raise HTTPException(404, "资源版本不存在或已删除")
    if version.available is False:
        raise HTTPException(404, "资源对象缺失，请重新上传或生成")
    return row, version

def execute_tool(name, arguments, db, access, workspace_id, conversation_id=None, request_id=None, skill_packages=None, check_active=None):
    check_workspace(access,workspace_id)
    if name=="list_files": return listing(workspace_id,arguments.get("query", ""),arguments.get("offset",0),arguments.get("limit",100),db,access)
    if name=="read_file":
        reference = arguments
        if not arguments.get("id"):
            row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
                WorkspaceFile.path == valid_path(arguments.get("path", "")), WorkspaceFile.deleted_at.is_(None)))
            if row is None: raise HTTPException(404, "资源不存在")
            reference = {**arguments, "id": str(row.id)}
        row, ver = resolve_input(db, access, reference)
        result = {**read_file(row), "version": ver.version, "size_bytes": ver.size_bytes,
            "content_type": ver.content_type, "sha256": ver.content_hash,
            "download_url": f"/resources/{row.id}/download?version={ver.version}",
            "preview_url": f"/resources/{row.id}/download?version={ver.version}&preview=true",
            "input": {"id": str(row.id), "version": ver.version}}
        if not ver.content_type.startswith("text/") and not row.path.lower().endswith((".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".css", ".sh")):
            return result
        from .resource_cache import read_bytes
        offset = min(arguments.get("offset", 0), ver.size_bytes)
        data = read_bytes(db, access, row, ver, offset=offset, limit=arguments.get("limit", 12000))
        return {**result, "content": data.decode("utf-8", errors="replace"), "offset": offset,
            "next_offset": offset + len(data) if offset + len(data) < ver.size_bytes else None}
    if name=="write_file":
        access.editor(); payload=TextWrite(path=arguments["path"],content=arguments["content"],expected_version=arguments.get("expected_version")); return write_text(workspace_id,payload,db,access)
    if name=="apply_patch":
        access.editor(); path = valid_path(arguments["path"])
        row = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
            WorkspaceFile.path == path, WorkspaceFile.deleted_at.is_(None)))
        if row is None: return {"error": "文件不存在"}
        if row.current_version != arguments["expected_version"]: raise HTTPException(409, "文件已被修改，请重新读取")
        version = _current_file_version(db, row)
        from .resource_cache import read_bytes
        content = read_bytes(db, access, row, version)
        try: updated = _apply_unified_patch(content.decode("utf-8"), arguments["patch"])
        except UnicodeDecodeError: return {"error": "只能对文本文件应用补丁"}
        return _save_bytes(workspace_id, access, db, path, updated.encode("utf-8"), row.content_type, arguments["expected_version"])
    if name=="delete_file":
        access.editor(); path=valid_path(arguments["path"]); row=db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==path,WorkspaceFile.deleted_at.is_(None)))
        if not row: return {"error":"文件不存在"}
        from .resources import remove
        remove(row.id, db, access)
        return {"path":path,"deleted":True}
    if name=="run_command":
        from .cloud_computer import execute
        return execute(db, access, conversation_id, request_id, arguments, skill_packages, check_active)
    return {"error":"未知文件工具"}
