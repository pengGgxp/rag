"use client";
import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, Download, FolderUp, Loader2, Save, Trash2, Upload } from "lucide-react";
import * as Tabs from "../../../../components/ui/tabs";
import { ApiError, downloadFile, request } from "../../../api";
import { WorkspaceHeader, WorkspaceSkeleton } from "../../../workspace-header";
import { useAdminChanges } from "../../../admin/admin-changes";
import { SkillResources } from "../../../admin/skills/skill-resources";
import { json, useExtensionResource, type PackageReview, type PersonalSkill, type SkillFile } from "../../extension-data";

type Draft = { name: string; description: string; markdown: string; enabled: boolean; files: SkillFile[]; limitations_acknowledged: boolean };
const empty: Draft = { name: "", description: "", markdown: "", enabled: false, files: [], limitations_acknowledged: false };
const draftOf = (item: PersonalSkill): Draft => ({ name: item.name, description: item.description, markdown: item.markdown,
  enabled: item.enabled, files: item.files ?? [], limitations_acknowledged: item.limitations_acknowledged ?? false });

export default function PersonalSkillPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const readonly = useSearchParams().has("platform");
  const router = useRouter();
  const base = readonly ? "/agent/skills/platform" : "/agent/skills";
  const resource = useExtensionResource<PersonalSkill>(id === "new" ? null : `${base}/${id}`);
  const [form, setForm] = useState<Draft>(empty);
  const [saved, setSaved] = useState<Draft>(empty);
  const [version, setVersion] = useState(0);
  const [tab, setTab] = useState("instructions");
  const [preview, setPreview] = useState(false);
  const [path, setPath] = useState("");
  const [review, setReview] = useState<PackageReview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [replacement, setReplacement] = useState<File[] | null>(null);
  const [folderMode, setFolderMode] = useState(false);
  const upload = useRef<HTMLInputElement>(null);
  const folder = useRef<HTMLInputElement>(null);
  const dirty = !readonly && (Object.keys(empty) as (keyof Draft)[]).some(key => form[key] !== saved[key]);
  useAdminChanges(dirty, busy);
  function accept(item: PersonalSkill) { const next = draftOf(item); setForm(next); setSaved(next); setVersion(item.version); setReview(item.review); }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  function edit(next: Partial<Draft>) {
    setForm(value => ({ ...value, ...next, ...("markdown" in next || "files" in next ? { limitations_acknowledged: false } : {}) }));
    if ("markdown" in next || "files" in next) setReview(undefined);
    setNotice("");
  }
  async function importFiles(files: File[], isFolder: boolean) {
    setBusy(true); setError(""); setReplacement(null);
    try {
      const body = new FormData();
      for (const file of files) {
        body.append(isFolder ? "files" : "file", file);
        if (isFolder) body.append("paths", file.webkitRelativePath);
      }
      const value = await request<PersonalSkill>(`${base}/${isFolder ? "import-folder" : "import"}`, { method: "POST", body });
      setForm(draftOf(value)); setReview(value.review); setTab("instructions"); setNotice("已导入，保存后生效");
    } catch (e) { setError(e instanceof Error ? e.message : "导入失败"); }
    finally { setBusy(false); }
  }
  function choose(files: File[], isFolder: boolean) {
    if (!files.length) return;
    if (files.length > 200 || files.reduce((sum, file) => sum + file.size, 0) > 25 * 1024 * 1024) {
      setError("Skill 包不能超过 25 MiB 或 200 个文件"); return;
    }
    setFolderMode(isFolder);
    if (dirty) setReplacement(files); else void importFiles(files, isFolder);
  }
  async function save(enable: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
      const checked = await request<PackageReview & { has_limitations: boolean }>(`${base}/review`, json({ markdown: form.markdown, files: form.files }));
      setReview(checked);
      if (enable && checked.has_limitations && !form.limitations_acknowledged) {
        setError("请先确认文件检查中列出的使用限制"); return;
      }
      const value = await request<PersonalSkill>(id === "new" ? base : `${base}/${id}`,
        json({ ...form, enabled: enable, ...(id === "new" ? {} : { version }) }, id === "new" ? "POST" : "PUT"));
      accept(value); setNotice("已保存"); setConflict(false);
      if (id === "new") router.replace(`/extensions/skills/${value.id}`);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); setConflict(e instanceof ApiError && e.status === 409); }
    finally { setBusy(false); }
  }
  return <>
    <WorkspaceHeader title={readonly ? saved.name || "平台 Skill" : id === "new" ? "添加 Skill" : saved.name || "编辑 Skill"} />
    <div className="workspace-body extension-page">
      <Link className="extension-back" href="/extensions"><ArrowLeft size={16} />Skills</Link>
      {resource.loading && id !== "new" ? <WorkspaceSkeleton /> : resource.error ? <div className="extension-error" role="alert">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div> :
      <form className="extension-form" onSubmit={e => { e.preventDefault(); void save(form.enabled); }}>
        <fieldset className="extension-fields" disabled={busy}>
        {!readonly && <div className="extension-actions">
          <button className="secondary" type="button" disabled={busy} onClick={() => upload.current?.click()}><Upload size={16} />导入 Skill 包</button>
          <button className="secondary" type="button" disabled={busy} onClick={() => folder.current?.click()}><FolderUp size={16} />导入文件夹</button>
          <input type="file" accept=".zip,.md" ref={upload} hidden onChange={e => { choose(Array.from(e.target.files ?? []), false); e.target.value = ""; }} />
          <input type="file" multiple {...{ webkitdirectory: "", directory: "" }} ref={folder} hidden onChange={e => { choose(Array.from(e.target.files ?? []), true); e.target.value = ""; }} />
          {id !== "new" && <button className="icon-button" type="button" aria-label="导出 Skill" title="导出 Skill" onClick={() => void downloadFile(`${base}/${id}/export`, "skill.zip").catch(e => setError(e.message))}><Download size={17} /></button>}
        </div>}
        {replacement && <div className="admin-inline-confirm" role="alert"><span>导入将替换当前未保存的内容。</span>
          <button className="secondary" type="button" onClick={() => setReplacement(null)}>继续编辑</button>
          <button className="danger" type="button" onClick={() => void importFiles(replacement, folderMode)}>替换草稿</button>
        </div>}
        <div className="extension-form-grid">
          <label>名称<input required maxLength={64} value={form.name} readOnly={readonly} onChange={e => edit({ name: e.target.value })} /></label>
          <label>用途<input required maxLength={1024} value={form.description} readOnly={readonly} onChange={e => edit({ description: e.target.value })} /></label>
        </div>
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List className="extension-tabs" aria-label="Skill 内容">
            <Tabs.Trigger value="instructions">指令</Tabs.Trigger><Tabs.Trigger value="files">文件{form.files.length ? ` · ${form.files.length}` : ""}</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="instructions">
            {!readonly && <label className="extension-check"><input type="checkbox" checked={preview} onChange={e => setPreview(e.target.checked)} />预览正文</label>}
            {preview || readonly ? <div className="skill-preview"><ReactMarkdown skipHtml components={{
              img: ({ alt }) => <span>{alt}</span>,
              a: ({ href, children }) => {
                let local = "";
                try { const link = new URL(href ?? "", "https://skill.local/"); if (link.origin === "https://skill.local") local = decodeURIComponent(link.pathname.slice(1)); } catch {}
                return form.files.some(item => item.path === local) ? <button type="button" className="skill-resource-link" onClick={() => { setPath(local); setTab("files"); }}>{children}</button>
                  : <span>{children}</span>;
              },
            }}>{form.markdown}</ReactMarkdown></div>
              : <label>Markdown 指令<textarea required maxLength={50000} value={form.markdown} onChange={e => edit({ markdown: e.target.value })} /></label>}
          </Tabs.Content>
          <Tabs.Content value="files"><SkillResources personal={!readonly} files={form.files} review={review} path={path} onSelect={setPath}
            onRemove={readonly ? undefined : selected => edit({ files: form.files.filter(item => item.path !== selected) })} /></Tabs.Content>
        </Tabs.Root>
        {!readonly && <section className="extension-section">
          <div className="extension-actions"><button className="secondary" type="button" disabled={busy || !form.markdown} onClick={async () => {
            setBusy(true); setError("");
            try { setReview(await request(`${base}/review`, json({ markdown: form.markdown, files: form.files }))); }
            catch (e) { setError(e instanceof Error ? e.message : "检查失败"); } finally { setBusy(false); }
          }}>检查文件</button><label className="extension-check"><input type="checkbox" checked={form.enabled} onChange={e => edit({ enabled: e.target.checked })} />启用</label></div>
        {review?.has_limitations && <div className="extension-warning">{!!review.issues.length && <ul>{review.issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
            <label className="extension-check"><input type="checkbox" checked={form.limitations_acknowledged} onChange={e => edit({ limitations_acknowledged: e.target.checked })} />我已了解：脚本会在当前工作区的临时隔离环境中运行，可能读取或生成工作区输出；缺少依赖或权限时任务可能无法完成。</label></div>}
        </section>}
        {error && <p role="alert" className="extension-error">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {conflict && <div className="admin-inline-confirm"><span>当前输入已保留。重新加载会放弃修改。</span><button type="button" className="danger" onClick={() => { void resource.reload(); setConflict(false); }}>放弃并重新加载</button></div>}
        {!readonly && <div className="extension-actions">
          <button className="primary" type="submit" disabled={busy || conflict}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存</button>
          {!form.enabled && <button className="secondary" type="button" disabled={busy || conflict} onClick={() => void save(true)}>保存并启用</button>}
          {dirty && <span className="muted">未保存</span>}
        </div>}
        {!readonly && id !== "new" && <div className="extension-section">
          {deleting ? <div className="admin-inline-confirm"><span>删除「{saved.name}」？已有回答不受影响。</span>
            <button className="secondary" type="button" onClick={() => setDeleting(false)}>保留</button>
            <button className="danger" type="button" disabled={busy} onClick={async () => { setBusy(true);
              try { await request(`${base}/${id}`, json({ version }, "DELETE")); router.push("/extensions"); }
              catch (e) { setError(e instanceof Error ? e.message : "删除失败"); } finally { setBusy(false); }
            }}>确认删除</button></div> : <button className="danger" type="button" onClick={() => setDeleting(true)}><Trash2 size={16} />删除 Skill</button>}
        </div>}
        </fieldset>
      </form>}
    </div>
  </>;
}
