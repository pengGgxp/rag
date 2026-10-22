"use client";

import { FormEvent, useRef, useState } from "react";
import { Check, Download, File, FileCode2, Loader2, Plus, Trash2, Upload } from "lucide-react";
import { downloadFile, request } from "./api";
import { Modal } from "./modal";
import type { Skill, SkillFile } from "./extensions";

const INITIAL_SKILL = "---\nname: research-assistant\ndescription: 梳理资料并输出有来源的研究摘要\n---\n\n# 研究助手\n\n先明确研究问题，再按需检索资料。\n区分已确认的事实和不确定的内容，保留来源。\n";
type DraftFile = Pick<SkillFile, "path" | "text" | "size"> & { content?: string; content_base64?: string };
const failure = (error: unknown) => error instanceof Error ? error.message : "文件操作失败";
const sizeLabel = (size: number) => size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`;
function encode(bytes: Uint8Array) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}
function downloadDraft(file: DraftFile) {
  const bytes = file.content_base64 !== undefined ? Uint8Array.from(atob(file.content_base64), c => c.charCodeAt(0))
    : new TextEncoder().encode(file.content ?? "");
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const link = document.createElement("a");
  link.href = url; link.download = file.path.split("/").pop() ?? "file"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function SkillBundleEditor({ skill, onClose, onSaved }: {
  skill?: Skill; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const [files, setFiles] = useState<DraftFile[]>(() => [
    { path: "SKILL.md", text: true, size: 0, content: skill?.markdown ?? INITIAL_SKILL },
    ...(skill?.files ?? []).filter(file => file.path !== "SKILL.md"),
  ]);
  const [selected, setSelected] = useState("SKILL.md");
  const [newPath, setNewPath] = useState("");
  const [enabled, setEnabled] = useState(skill?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const upload = useRef<HTMLInputElement>(null);
  const current = files.find(file => file.path === selected)!;
  const choose = async (file: DraftFile) => {
    setSelected(file.path); setError("");
    if (!file.text || file.content !== undefined || !skill) return;
    setReading(true);
    try {
      const loaded = await request<{ content: string }>(`/agent/skills/${skill.id}/file?path=${encodeURIComponent(file.path)}`);
      setFiles(items => items.map(item => item.path === file.path && item.content === undefined ? { ...item, content: loaded.content } : item));
    } catch (reason) { setError(failure(reason)); }
    finally { setReading(false); }
  };
  const add = () => {
    const path = newPath.trim();
    if (!path) return;
    if (files.some(file => file.path.toLowerCase() === path.toLowerCase())) {
      setError("文件已存在，请从目录中选择"); return;
    }
    setFiles(items => [...items, { path, text: true, size: 0, content: "" }]);
    setSelected(path); setNewPath(""); setError("");
  };
  const uploadFiles = async (incoming: File[]) => {
    setBusy(true); setError("");
    try {
      if (incoming.some(file => file.size > 5 * 1024 * 1024)) throw new Error("单个文件不能超过 5 MiB");
      const next: DraftFile[] = [];
      for (const file of incoming) {
        const path = incoming.length === 1 && newPath.trim() ? newPath.trim() : file.name;
        if (path.toLowerCase() === "skill.md") throw new Error("请在编辑器中修改 SKILL.md；完整 Skill 请从列表导入");
        const bytes = new Uint8Array(await file.arrayBuffer());
        let text: string | undefined;
        try {
          const decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
          if (!/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(decoded)) text = decoded;
        } catch { /* Binary assets remain downloadable without text conversion. */ }
        next.push({ path, size: bytes.length, text: text !== undefined, content: text, content_base64: encode(bytes) });
      }
      setFiles(items => [...items.filter(item => !next.some(file => file.path === item.path)), ...next]);
      if (next[0]) setSelected(next[0].path);
      setNewPath("");
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(false); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const markdown = files.find(file => file.path === "SKILL.md")!.content!;
      await request(`/agent/skills${skill ? `/${skill.id}` : ""}`, {
        method: skill ? "PUT" : "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          markdown, enabled, files: files.filter(file => file.path !== "SKILL.md").map(file => ({
            path: file.path,
            ...(file.content_base64 !== undefined ? { content_base64: file.content_base64 } : file.content !== undefined
              ? { content_base64: encode(new TextEncoder().encode(file.content)) }
              : {}),
          })),
        }),
      });
      await onSaved();
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(false); }
  };
  const download = async () => {
    setBusy(true); setError("");
    try {
      if (skill && current.content === undefined && current.content_base64 === undefined)
        await downloadFile(`/agent/skills/${skill.id}/file?path=${encodeURIComponent(current.path)}&download=true`, current.path.split("/").pop()!);
      else downloadDraft(current);
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(false); }
  };
  return <Modal title={skill ? `编辑 ${skill.name}` : "创建 Skill"} wide busy={busy || reading} onClose={onClose}>
    <form className="extension-form skill-bundle-form" onSubmit={submit}>
      <div className="skill-file-toolbar">
        <input aria-label="新文件路径" placeholder="references/guide.md" value={newPath} disabled={busy} onChange={e => setNewPath(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        <button className="icon-button" type="button" title="新建文本文件" aria-label="新建文本文件" disabled={busy || !newPath.trim()} onClick={add}><Plus size={16} /></button>
        <button className="icon-button" type="button" title="上传文件（填写路径可替换指定文件）" aria-label="上传 Skill 文件" disabled={busy} onClick={() => upload.current?.click()}><Upload size={16} /></button>
      </div>
      <input ref={upload} hidden type="file" multiple onChange={e => {
        const incoming = Array.from(e.currentTarget.files ?? []); e.currentTarget.value = "";
        if (incoming.length) void uploadFiles(incoming);
      }} />
      <div className="skill-file-workspace">
        <nav className="skill-file-list" aria-label="Skill 文件">
          {[...files].sort((a, b) => a.path === "SKILL.md" ? -1 : b.path === "SKILL.md" ? 1 : a.path.localeCompare(b.path)).map(file =>
            <button key={file.path} type="button" className={selected === file.path ? "selected" : ""} aria-current={selected === file.path ? "true" : undefined}
              title={file.path} disabled={busy || reading} onClick={() => void choose(file)}>
              {file.text ? <FileCode2 size={15} /> : <File size={15} />}<span>{file.path}</span>
            </button>,
          )}
        </nav>
        <section className="skill-file-content">
          <div className="skill-file-heading"><strong>{current.path}</strong><div>
            <button className="icon-button" type="button" title="下载文件" aria-label="下载文件" disabled={busy || reading} onClick={() => void download()}><Download size={15} /></button>
            <button className="icon-button" type="button" title="删除文件" aria-label="删除当前文件" disabled={busy || current.path === "SKILL.md"} onClick={() => {
              setFiles(items => items.filter(file => file.path !== selected)); setSelected("SKILL.md");
            }}><Trash2 size={15} /></button>
          </div></div>
          {reading ? <div className="skill-binary-preview"><Loader2 size={20} className="spin" /></div> : current.text
            ? current.content === undefined
              ? <div className="skill-binary-preview"><button className="secondary" type="button" onClick={() => void choose(current)}>重新读取文件</button></div>
              : <textarea className="skill-editor" aria-label={current.path} spellCheck={false} disabled={busy} value={current.content} onChange={e => {
                const content = e.target.value;
                setFiles(items => items.map(file => file.path === selected ? { ...file, content, content_base64: undefined } : file));
              }} />
            : <div className="skill-binary-preview"><File size={30} /><strong>{current.path.split("/").pop()}</strong><span>{sizeLabel(current.size)}</span><span>二进制文件</span></div>}
        </section>
      </div>
      <div className="skill-bundle-footer"><label className="extension-checkbox"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用</label><span>{files.length} 个文件</span></div>
      {files.some(file => file.path.startsWith("scripts/")) && <p className="skill-runtime-note">脚本仅保存，不在服务器执行。</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="modal-footer"><button className="secondary" type="button" disabled={busy || reading} onClick={onClose}>取消</button><button className="primary" type="submit" disabled={busy || reading}>{busy ? <Loader2 size={15} className="spin" /> : <Check size={15} />}保存</button></div>
    </form>
  </Modal>;
}
