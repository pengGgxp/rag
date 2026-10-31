"use client";
import { useEffect, useState } from "react";
import { Download, File, RefreshCw, Search } from "lucide-react";
import { request, API_URL } from "../api";
import { WorkspaceHeader } from "../workspace-header";
import { WorkspaceShell } from "../workspace-shell";
import { useAuth } from "../auth";

type WorkspaceFile = { id:string; path:string; content_type:string; size_bytes:number; version:number; updated_at:string };
function humanSize(value:number) { return value < 1024 ? `${value} B` : value < 1024*1024 ? `${(value/1024).toFixed(1)} KB` : `${(value/1024/1024).toFixed(1)} MB`; }
export default function WorkspaceFilesPage() {
  const { session } = useAuth(); const [items,setItems]=useState<WorkspaceFile[]>([]); const [query,setQuery]=useState(""); const [error,setError]=useState("");
  const load=async()=>{ try { const data=await request<{items:WorkspaceFile[]}>(`/workspaces/${session.workspace_id}/files?q=${encodeURIComponent(query)}`); setItems(data.items); setError(""); } catch(e) { setError(e instanceof Error?e.message:"文件加载失败"); } };
  useEffect(()=>{ void load(); },[session.workspace_id,query]);
  return <WorkspaceShell screen="files"><WorkspaceHeader title="工作区输出"/><div className="workspace-body workspace-files-page"><div className="workspace-files-toolbar"><label className="extension-search"><Search size={16}/><input placeholder="搜索输出文件" value={query} onChange={e=>setQuery(e.target.value)}/></label><button className="icon-button" onClick={()=>void load()} title="刷新"><RefreshCw size={16}/></button></div>{error&&<div className="workspace-request-error" role="alert">{error}</div>}<p className="workspace-files-hint">这里显示 Skill 和对话生成的临时文件，系统会定期清理。</p><div className="workspace-file-list">{items.map(item=><div className="workspace-file-row" key={item.id}><File size={19}/><div><strong>{item.path}</strong><small>{humanSize(item.size_bytes)} · 最近生成于 {new Date(item.updated_at).toLocaleString()}</small></div><a className="icon-button" href={`${API_URL}/workspaces/${session.workspace_id}/files/${item.id}/download`} title="下载"><Download size={16}/></a></div>)}{!items.length&&!error&&<p className="extension-empty">暂时没有生成文件</p>}</div></div></WorkspaceShell>;
}
