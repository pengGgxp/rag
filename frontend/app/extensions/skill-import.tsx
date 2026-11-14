"use client";
import Link from "next/link";
import "./extensions.css";
import "../resource-library.css";
import { useRef, useState } from "react";
import { FolderUp, Upload } from "lucide-react";
import { request } from "../api";
import { useAuth } from "../auth";
import { ResourceNotice } from "../resource-notice";
import { ResourcePreview } from "../resource-preview";
import type { ResourceFile } from "../resource-library";
import { useExtensionResource } from "./extension-data";

export function SkillImport({ id }: { id?: string }) {
  const { session, role } = useAuth();
  const input = useRef<HTMLInputElement>(null), folder = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [items, setItems] = useState<ResourceFile[]>([]);
  const [directory, setDirectory] = useState("");
  const [preview, setPreview] = useState<ResourceFile | null>(null);
  const resource = useExtensionResource<ResourceFile>(id ? `/agent/skills/${id}` : null);
  const canWrite = role === "admin" || role === "editor";
  async function upload(files: File[], isFolder: boolean) {
    if (!files.length || busy) return;
    setBusy(true); setError(""); setItems([]); setDirectory("");
    try {
      const body = new FormData();
      for (const file of files) {
        body.append(isFolder ? "files" : "file", file);
        if (isFolder) body.append("paths", file.webkitRelativePath);
      }
      const result = await request<{ items: ResourceFile[]; directory: string }>(`/agent/skills/import${isFolder ? "-folder" : ""}`, { method: "POST", body });
      setItems(result.items); setDirectory(result.directory);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "导入失败，请重试"); }
    finally { setBusy(false); }
  }
  return <section className="extension-section skill-import" aria-label="导入 Skill">
    <ResourceNotice />
    {canWrite && <div className="extension-actions">
      <input ref={input} hidden type="file" accept=".md,.zip" onChange={event => { void upload(Array.from(event.target.files ?? []), false); event.target.value = ""; }} />
      <input ref={folder} hidden type="file" multiple {...{ webkitdirectory: "" }} onChange={event => { void upload(Array.from(event.target.files ?? []), true); event.target.value = ""; }} />
      <button type="button" className="primary" disabled={busy} onClick={() => input.current?.click()}><Upload size={16} />{busy ? "正在导入" : "导入 Skill 包"}</button>
      <button type="button" className="secondary" disabled={busy} onClick={() => folder.current?.click()}><FolderUp size={16} />导入文件夹</button>
    </div>}
    {(error || resource.error) && <p className="extension-error" role="alert">{error || resource.error}</p>}
    {resource.error && <button type="button" className="secondary" onClick={() => void resource.reload()}>重试</button>}
    {resource.data && <button type="button" className="text-button" data-resource-id={resource.data.id} onClick={() => setPreview(resource.data!)}>{resource.data.path}</button>}
    {!!items.length && <><p role="status">已导入 {items.length} 个资源文件</p>
      <ul>{items.map(item => <li key={item.id}><button type="button" className="text-button" data-resource-id={item.id} onClick={() => setPreview(item)}>{item.path}</button></li>)}</ul></>}
    <Link href={`/resources${directory ? `?path=${encodeURIComponent(directory)}` : ""}`}>在工作区查看</Link>
    {preview && <ResourcePreview item={preview} scope={`${session.user.id}:${session.workspace_id}`} onClose={() => setPreview(null)} />}
  </section>;
}
