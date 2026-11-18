"use client";
import Link from "next/link";
import "./extensions.css";
import "../resource-library.css";
import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Check, FolderUp, Loader2, Upload, X } from "lucide-react";
import { request } from "../api";
import { useAuth } from "../auth";
import { ResourceNotice } from "../resource-notice";
import { ResourcePreview } from "../resource-preview";
import type { ResourceFile } from "../resource-library";
import { useExtensionResource } from "./extension-data";
import { skillUploadError } from "./skill-upload";
import { uploadSkillPackage, zipSkillFolder, type SkillPackagePreview } from "./skill-package-upload";

export function SkillImport({ id }: { id?: string }) {
  const { session, role } = useAuth();
  const input = useRef<HTMLInputElement>(null), folder = useRef<HTMLInputElement>(null);
  const [operation, setOperation] = useState<"upload" | "import" | null>(null), [error, setError] = useState("");
  const busy = operation !== null;
  const [items, setItems] = useState<ResourceFile[]>([]);
  const [directory, setDirectory] = useState("");
  const [preview, setPreview] = useState<ResourceFile | null>(null);
  const [packages, setPackages] = useState<SkillPackagePreview[]>([]);
  const [uploadId, setUploadId] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [activePackage, setActivePackage] = useState(0);
  const resource = useExtensionResource<ResourceFile>(id ? `/agent/skills/${id}` : null);
  const canWrite = role === "admin" || role === "editor";
  async function upload(files: File[], isFolder: boolean) {
    if (!files.length || busy) return;
    const error = skillUploadError(files, isFolder);
    if (error) { setError(error); return; }
    setOperation("upload"); setError(""); setItems([]); setDirectory("");
    try {
      const file = isFolder ? await zipSkillFolder(files) : files[0];
      const result = await uploadSkillPackage(file, "agent");
      setPackages(result.items); setUploadId(result.uploadId);
      setSelected(result.items.map(item => item.id)); setActivePackage(0);
      if (!result.items.length) throw new Error("没有识别到 SKILL.md");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "导入失败，请重试"); }
    finally { setOperation(null); }
  }
  async function confirmImport() {
    if (!uploadId || !selected.length || busy) return;
    setOperation("import"); setError("");
    try {
      const result = await request<{ items: { items: ResourceFile[]; directory: string }[] }>(
        `/agent/skills/uploads/${uploadId}/import`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: selected }) },
      );
      const imported = result.items.flatMap(item => item.items);
      setItems(imported); setDirectory(result.items[0]?.directory ?? "");
      setPackages([]); setUploadId(""); setSelected([]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "导入失败，请重试"); }
    finally { setOperation(null); }
  }
  async function discardPreview() {
    if (uploadId) await request(`/agent/skills/uploads/${uploadId}`, { method: "DELETE" }).catch(() => undefined);
    setPackages([]); setUploadId(""); setSelected([]);
  }
  const currentPackage = packages.find(item => item.id === activePackage);
  return <section className="extension-section skill-import" aria-label="导入 Skill">
    <ResourceNotice />
    {canWrite && <div className="extension-actions">
      <input ref={input} hidden type="file" accept=".md,.zip" onChange={event => { void upload(Array.from(event.target.files ?? []), false); event.target.value = ""; }} />
      <input ref={folder} hidden type="file" multiple {...{ webkitdirectory: "" }} onChange={event => { void upload(Array.from(event.target.files ?? []), true); event.target.value = ""; }} />
      <button type="button" className="primary" disabled={busy || !!uploadId} onClick={() => input.current?.click()}><Upload size={16} />{operation === "upload" ? "正在上传并识别" : "选择 Skill 包"}</button>
      <button type="button" className="secondary" disabled={busy || !!uploadId} onClick={() => folder.current?.click()}><FolderUp size={16} />选择文件夹</button>
    </div>}
    {!!packages.length && <section className="skill-package-review" aria-label="识别到的 Skill" aria-busy={busy}>
      <header><strong>识别到 {packages.length} 个 Skill</strong><span>选择要导入的项目并查看正文</span></header>
      <div className="skill-package-review-body">
        <nav aria-label="Skill 列表">{packages.map(item => <div className="skill-package-choice" key={item.id}>
          <label><input type="checkbox" checked={selected.includes(item.id)} aria-label={`导入${item.name}`}
            onChange={event => setSelected(values => event.target.checked
              ? [...values, item.id] : values.filter(value => value !== item.id))} />
          <span><strong>{item.name}</strong><small>{item.description || item.folder}</small></span>
          </label><button type="button" aria-current={item.id === activePackage ? "true" : undefined}
            aria-label={`查看${item.name}正文`} onClick={() => setActivePackage(item.id)}>预览</button>
        </div>)}</nav>
        {currentPackage && <article>
          <h3>{currentPackage.name}</h3>
          <p>{currentPackage.description}</p>
          <div className="skill-package-markdown"><ReactMarkdown skipHtml>{currentPackage.markdown}</ReactMarkdown></div>
          {!!currentPackage.resources.length && <details><summary>附属文件 · {currentPackage.resources.length}</summary>
            <ul>{currentPackage.resources.map(file => <li key={file.path}>{file.path}</li>)}</ul>
          </details>}
        </article>}
      </div>
      <footer><button type="button" className="secondary" disabled={busy} onClick={() => void discardPreview()}><X size={15} />取消</button>
        <button type="button" className="primary" disabled={busy || !selected.length} onClick={() => void confirmImport()}>
          {operation === "import" ? <Loader2 size={15} className="spin" /> : <Check size={15} />}
          {operation === "import" ? "正在导入" : `导入所选 ${selected.length} 个 Skill`}
        </button></footer>
    </section>}
    {(error || resource.error) && <p className="extension-error" role="alert">{error || resource.error}</p>}
    {resource.error && <button type="button" className="secondary" onClick={() => void resource.reload()}>重试</button>}
    {resource.data && <button type="button" className="text-button" data-resource-id={resource.data.id} onClick={() => setPreview(resource.data!)}>{resource.data.path}</button>}
    {!!items.length && <><p role="status">已导入 {items.length} 个资源文件</p>
      <ul>{items.map(item => <li key={item.id}><button type="button" className="text-button" data-resource-id={item.id} onClick={() => setPreview(item)}>{item.path}</button></li>)}</ul></>}
    <Link href={`/resources${directory ? `?path=${encodeURIComponent(directory)}` : ""}`}>在工作区查看</Link>
    {preview && <ResourcePreview item={preview} scope={`${session.user.id}:${session.workspace_id}`} onClose={() => setPreview(null)}
      restoreFocusSelector={`[data-resource-id="${preview.id}"]`} />}
  </section>;
}
