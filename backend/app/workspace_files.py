import base64, hashlib, json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from .auth import Access, get_access, now
from .db import get_db
from .models import WorkspaceFile, WorkspaceFileVersion, WorkspaceExecutionJob
from .storage import download_fileobj, upload_fileobj
from .config import get_settings

router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["workspace-files"])
MAX_BYTES = 200 * 1024 * 1024
MAX_OUTPUT = 12000
def valid_path(path: str) -> str:
    if not path or len(path) > 512 or path.startswith(("/", "\\")) or "\\" in path:
        raise HTTPException(422, "文件路径无效")
    p = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in p.parts): raise HTTPException(422, "文件路径无效")
    return str(p)
def check_workspace(access, workspace_id):
    if UUID(str(workspace_id)) != access.workspace_id: raise HTTPException(404, "工作区不存在或无权访问")
def read_file(row):
    return {"id": str(row.id), "path": row.path, "content_type": row.content_type, "size_bytes": row.size_bytes, "sha256": row.content_hash, "version": row.current_version, "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}
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
@router.get("")
def listing(workspace_id: UUID, q: str = "", offset: int = 0, limit: int = 100, db=Depends(get_db), access: Access=Depends(get_access)):
    check_workspace(access, workspace_id); limit=min(max(limit,1),500); rows=db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id, WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.ilike(f"%{q}%")).order_by(WorkspaceFile.path).offset(max(offset,0)).limit(limit)).all(); return {"items":[read_file(r) for r in rows],"offset":offset,"limit":limit}
@router.get("/{file_id}")
def detail(workspace_id: UUID, file_id: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    return read_file(owned(db,access,workspace_id,file_id))
@router.post("/upload", status_code=201)
def upload(workspace_id: UUID, file: UploadFile=File(...), db=Depends(get_db), access: Access=Depends(get_access)):
    check_workspace(access,workspace_id); access.editor(); path=valid_path(file.filename or "")
    data=file.file.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise HTTPException(413,"文件不能超过 200 MiB")
    digest=hashlib.sha256(data).hexdigest(); row=db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==path))
    if row and not row.deleted_at: version=row.current_version+1; fid=row.id
    else: row=WorkspaceFile(workspace_id=access.workspace_id,path=path,content_type=file.content_type or "application/octet-stream",size_bytes=len(data),content_hash=digest,current_version=1,created_by=access.user_id,updated_by=access.user_id); db.add(row); db.flush(); version=1; fid=row.id
    key=f"workspaces/{access.workspace_id}/files/{fid}/{version}/{path}"; upload_fileobj(__import__('io').BytesIO(data),key,row.content_type)
    row.current_version=version; row.size_bytes=len(data); row.content_hash=digest; row.updated_by=access.user_id; row.deleted_at=None; db.add(WorkspaceFileVersion(file_id=fid,workspace_id=access.workspace_id,version=version,object_key=key,content_type=row.content_type,size_bytes=len(data),content_hash=digest,created_by=access.user_id)); db.commit(); return read_file(row)
@router.post("/text", status_code=201)
def write_text(workspace_id: UUID, payload: TextWrite, db=Depends(get_db), access: Access=Depends(get_access)):
    access.editor(); valid_path(payload.path); row=db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==payload.path))
    if row and payload.expected_version != row.current_version: raise HTTPException(409,"文件已被修改，请重新读取")
    from starlette.datastructures import UploadFile as _U
    data=payload.content.encode(); fake=type("F",(),{"filename":payload.path,"content_type":payload.content_type,"file":__import__('io').BytesIO(data)})(); return upload(workspace_id,fake,db,access)
@router.delete("/{file_id}", status_code=204)
def delete(workspace_id: UUID,file_id: UUID,db=Depends(get_db),access: Access=Depends(get_access)):
    row=owned(db,access,workspace_id,file_id,True); row.deleted_at=now(); db.commit(); return None
@router.get("/{file_id}/download")
def download(workspace_id: UUID,file_id: UUID,db=Depends(get_db),access: Access=Depends(get_access)):
    from fastapi.responses import StreamingResponse
    row=owned(db,access,workspace_id,file_id); version=db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id==row.id,WorkspaceFileVersion.version==row.current_version)); stream=download_fileobj(version.object_key); return StreamingResponse(stream,media_type=row.content_type,headers={"Content-Disposition":f'attachment; filename="{row.path.split("/")[-1]}"'})
@router.get("/{file_id}/versions")
def versions(workspace_id: UUID, file_id: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    row=owned(db,access,workspace_id,file_id)
    return [{"version": item.version, "size_bytes": item.size_bytes, "sha256": item.content_hash, "created_at": item.created_at.isoformat(), "current": item.version == row.current_version} for item in row.versions]

def tool_definitions():
    return [{"type":"function","function":{"name":n,"description":d,"parameters":p}} for n,d,p in [
        ("list_files","列出当前工作区文件",{"type":"object","properties":{"query":{"type":"string"}},"additionalProperties":False}),
        ("read_file","读取当前工作区文本文件",{"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer","minimum":0}},"required":["path"],"additionalProperties":False}),
        ("write_file","创建或覆盖当前工作区文本文件",{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"expected_version":{"type":"integer"}},"required":["path","content"],"additionalProperties":False}),
        ("delete_file","删除当前工作区文件",{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":False}),
        ("run_command","在当前工作区隔离环境执行 Python、Node 或 sh 命令",{"type":"object","properties":{"command":{"type":"string","maxLength":2000}},"required":["command"],"additionalProperties":False}),
    ]]

def execute_tool(name, arguments, db, access, workspace_id, conversation_id=None, request_id=None):
    check_workspace(access,workspace_id)
    if name=="list_files": return listing(workspace_id,arguments.get("query", ""),0,100,db,access)
    rows=db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==valid_path(arguments["path"]),WorkspaceFile.deleted_at.is_(None))).all() if name=="read_file" else []
    if name=="read_file":
        if not rows:return {"error":"文件不存在"}
        row=rows[0]; ver=db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id==row.id,WorkspaceFileVersion.version==row.current_version)); stream=download_fileobj(ver.object_key); content=stream.read(12000); return {"path":row.path,"version":row.current_version,"content":content.decode("utf-8",errors="replace")}
    if name=="write_file":
        access.editor(); payload=TextWrite(path=arguments["path"],content=arguments["content"],expected_version=arguments.get("expected_version")); return write_text(workspace_id,payload,db,access)
    if name=="delete_file":
        access.editor(); path=valid_path(arguments["path"]); row=db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==path,WorkspaceFile.deleted_at.is_(None)))
        if not row: return {"error":"文件不存在"}
        row.deleted_at=now(); db.commit(); return {"path":path,"deleted":True}
    if name=="run_command":
        access.editor(); settings=get_settings()
        if not settings.workspace_executor_url: return {"error":"脚本执行环境尚未配置，请联系站点管理员"}
        import httpx
        files={}
        for row in db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.deleted_at.is_(None))).all():
            version=db.scalar(select(WorkspaceFileVersion).where(WorkspaceFileVersion.file_id==row.id,WorkspaceFileVersion.version==row.current_version))
            stream=download_fileobj(version.object_key); files[row.path]=base64.b64encode(stream.read(MAX_BYTES+1)).decode()
        payload={"workspace_id":str(access.workspace_id),"command":arguments["command"],"files":files}
        job=WorkspaceExecutionJob(workspace_id=access.workspace_id,user_id=access.user_id,conversation_id=conversation_id,request_id=request_id,command_digest=hashlib.sha256(arguments["command"].encode()).hexdigest())
        db.add(job); db.commit()
        try:
            response=httpx.post(f"{settings.workspace_executor_url.rstrip('/')}/run",json=payload,timeout=90)
            response.raise_for_status(); result=response.json()
        except Exception:
            job.status="failed"; job.stderr_summary="隔离执行环境暂时不可用"; job.finished_at=now(); db.commit()
            return {"error":"隔离执行环境暂时不可用，脚本未执行"}
        job.status=result.get("status", "failed"); job.exit_code=result.get("exit_code"); job.stdout_summary=str(result.get("stdout", ""))[-MAX_OUTPUT:]; job.stderr_summary=str(result.get("stderr", ""))[-MAX_OUTPUT:]; job.finished_at=now(); db.commit()
        for path, encoded in result.pop("files", {}).items():
            try:
                data=base64.b64decode(encoded); current=db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id==access.workspace_id,WorkspaceFile.path==path,WorkspaceFile.deleted_at.is_(None)))
                if current and current.content_hash == hashlib.sha256(data).hexdigest(): continue
                payload=TextWrite(path=path,content=data.decode("utf-8"),expected_version=current.current_version if current else None); write_text(workspace_id,payload,db,access)
            except Exception:
                result.setdefault("warnings", []).append(f"无法保存文件：{path}")
        return result
    return {"error":"未知文件工具"}
