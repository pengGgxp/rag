"use client";
import { useEffect, useState } from "react";
import { Download, File, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { request, API_URL } from "../api";
import { WorkspaceHeader } from "../workspace-header";
import { WorkspaceShell } from "../workspace-shell";
import { useAuth } from "../auth";

type WorkspaceFile = { id:string; path:string; content_type:string; size_bytes:number; version:number; updated_at:string };
function humanSize(value:number) { return value < 1024 ? `${value} B` : value < 1024*1024 ? `${(value/1024).toFixed(1)} KB` : `${(value/1024/1024).toFixed(1)} MB`; }
export default function WorkspaceFilesPage() {
  const { session } = useAuth(); const [items,setItems]=useState<WorkspaceFile[]>([]); const [query,setQuery]=useState(""); const [busy,setBusy]=useState(false); const [error,setError]=useState("");
  const load=async()=>{ try { const data=await request<{items:WorkspaceFile[]}>(`/workspaces/${session.workspace_id}/files?q=${encodeURIComponent(query)}`); setItems(data.items); setError(""); } catch(e) { setError(e instanceof Error?e.message:"文件加载失败"); } };
  useEffect(()=>{ void load(); },[session.workspace_id,query]);
  async function upload(event:React.ChangeEvent<HTMLInputElement>) { const file=event.target.files?.[0]; if(!file)return; setBusy(true); try { const form=new FormData(); form.append("file",file); await request(`/workspaces/${session.workspace_id}/files/upload`,{method:"POST",body:form}); await load(); } catch(e) { setError(e instanceof Error?e.message:"上传失败"); } finally { setBusy(false); event.target.value=""; } }
  async function remove(id:string) { if(!confirm("删除这个文件？"))return; try { await request(`/workspaces/${session.workspace_id}/files/${id}`,{method:"DELETE"}); await load(); } catch(e) { setError(e instanceof Error?e.message:"删除失败"); } }
  const canEdit=session.workspaces.find(item=>item.id===session.workspace_id)?.role!=="viewer";
  return <WorkspaceShell screen="files"><WorkspaceHeader title="工作区文件" actions={<label className="primary workspace-file-upload"><Upload size={16}/>上传文件<input type="file" hidden onChange={upload} disabled={!canEdit||busy}/></label>}/><div className="workspace-body workspace-files-page"><div className="workspace-files-toolbar"><label className="extension-search"><Search size={16}/><input placeholder="搜索文件" value={query} onChange={e=>setQuery(e.target.value)}/></label><button className="icon-button" onClick={()=>void load()} title="刷新"><RefreshCw size={16}/></button></div>{error&&<div className="workspace-request-error" role="alert">{error}</div>}<div className="workspace-file-list">{items.map(item=><div className="workspace-file-row" key={item.id}><File size={19}/><div><strong>{item.path}</strong><small>{humanSize(item.size_bytes)} · 第 {item.version} 版 · {new Date(item.updated_at).toLocaleString()}</small></div><a className="icon-button" href={`${API_URL}/workspaces/${session.workspace_id}/files/${item.id}/download`} title="下载"><Download size={16}/></a>{canEdit&&<button className="icon-button danger" onClick={()=>void remove(item.id)} title="删除"><Trash2 size={16}/></button>}</div>)}{!items.length&&!error&&<p className="extension-empty">工作区还没有文件</p>}</div></div></WorkspaceShell>;
}
