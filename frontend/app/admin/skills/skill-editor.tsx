"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, Check, Loader2, Save, Trash2, Upload, X } from "lucide-react";
import * as Tabs from "../../../components/ui/tabs";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import type { PackageReview, Skill } from "./page";
import { SkillResources } from "./skill-resources";
import { skillUploadError } from "../../extensions/skill-upload";
import { uploadSkillPackage, type SkillPackagePreview } from "../../extensions/skill-package-upload";
import "../../billing.css";
import "../platform-content.css";

type Draft = Pick<Skill, "name" | "description" | "markdown" | "enabled" | "files">;
const empty: Draft = { name: "", description: "", markdown: "", enabled: false, files: [] };
const draftOf = ({ name, description, markdown, enabled, files }: Skill): Draft => ({ name, description, markdown, enabled, files: files ?? [] });
const checkedPackage = (draft: Draft, result: PackageReview) => ({ markdown: draft.markdown, files: draft.files, result });

export function SkillEditor({ id }: { id?: string }) {
  const resource = useAdminResource<Skill>(id ? `/site/admin/skills/${id}` : null);
  const router = useRouter();
  const [form, setForm] = useState<Draft>(empty);
  const [saved, setSaved] = useState<Draft>(empty);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [tab, setTab] = useState("edit");
  const [resourcePath, setResourcePath] = useState("");
  const [checked, setChecked] = useState<ReturnType<typeof checkedPackage>>();
  const [packages, setPackages] = useState<SkillPackagePreview[]>([]);
  const [uploadId, setUploadId] = useState("");
  const [selectedPackages, setSelectedPackages] = useState<number[]>([]);
  const [activePackage, setActivePackage] = useState(0);
  const packageReview = checked?.markdown === form.markdown && checked?.files === form.files ? checked.result : undefined;
  const input = useRef<HTMLInputElement>(null);
  const dirty = form.name !== saved.name || form.description !== saved.description ||
    form.markdown !== saved.markdown || form.enabled !== saved.enabled || form.files !== saved.files;
  useAdminChanges(dirty, busy);
  function accept(value: Skill) { const draft = draftOf(value); setForm(draft); setSaved(draft); setVersion(value.version);
    if (value.review) setChecked(checkedPackage(draft, value.review)); }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function save(enable?: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await request<PackageReview>("/site/admin/skills/review", json({ markdown: form.markdown, files: form.files }));
      setChecked(checkedPackage(form, result));
      if ((enable ?? form.enabled) && !result.can_enable) throw new Error("请先处理文件检查中列出的问题，再启用 Skill。");
      const body = { ...form, enabled: enable ?? form.enabled, ...(id ? { version } : {}) };
      const value = await request<Skill>(id ? `/site/admin/skills/${id}` : "/site/admin/skills", json(body, id ? "PUT" : "POST"));
      accept(value); setNotice("已保存"); setConflict(false);
      if (!id) router.replace(`/admin/skills/${value.id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally { setBusy(false); }
  }
  return <>
    <Link className="admin-back" href="/admin/skills"><ArrowLeft size={16} />Skill 管理</Link>
    <AdminHeading title={id ? saved.name || "编辑 Skill" : "新建 Skill"}>
      <input ref={input} type="file" accept=".md,.zip" hidden onChange={async event => {
        const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
        const error = skillUploadError([file]);
        if (error) { setError(error); return; }
        setBusy(true); setError(""); setNotice("");
        try {
          const result = await uploadSkillPackage(file, "site");
          setPackages(result.items); setUploadId(result.uploadId);
          setSelectedPackages(result.items.map(item => item.id)); setActivePackage(result.items[0]?.id ?? 0);
          setNotice(`识别到 ${result.items.length} 个 Skill，请检查正文并确认导入`);
        } catch (reason) { setError(reason instanceof Error ? reason.message : "导入失败，请重试"); }
        finally { setBusy(false); }
      }} />
      <button className="secondary" disabled={busy || resource.loading} onClick={() => {
        if (form.markdown || form.files.length || form.name || form.description) setReplacing(true);
        else input.current?.click();
      }}><Upload size={16} />导入 Skill 包</button>
    </AdminHeading>
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {error && <p className="admin-error" role="alert">{error}</p>}
      {notice && <p className="billing-notice" role="status">{notice}</p>}
      {!!packages.length && <section className="skill-package-review" aria-label="识别到的 Skill">
        <header><strong>识别到 {packages.length} 个 Skill</strong><span>检查每个 Skill 的名称、描述和完整正文</span></header>
        <div className="skill-package-review-body">
        <nav aria-label="Skill 列表">{packages.map(item => <div className="skill-package-choice" key={item.id}>
            <label><input type="checkbox" checked={selectedPackages.includes(item.id)} aria-label={`导入${item.name}`}
              onChange={event => setSelectedPackages(values => event.target.checked
                ? [...values, item.id] : values.filter(value => value !== item.id))} />
            <span><strong>{item.name}</strong><small>{item.description || item.folder}</small></span>
            </label><button type="button" aria-current={item.id === activePackage ? "true" : undefined}
              aria-label={`查看${item.name}正文`} onClick={() => setActivePackage(item.id)}>预览</button>
          </div>)}</nav>
          {packages.find(item => item.id === activePackage) && <article>
            <h3>{packages[activePackage]?.name}</h3>
            <p>{packages[activePackage]?.description}</p>
            <div className="skill-package-markdown"><ReactMarkdown skipHtml>{packages[activePackage]?.markdown}</ReactMarkdown></div>
            {!!packages[activePackage]?.resources.length && <details><summary>附属文件 · {packages[activePackage].resources.length}</summary>
              <ul>{packages[activePackage].resources.map(file => <li key={file.path}>{file.path}</li>)}</ul>
            </details>}
          </article>}
        </div>
        <footer><button type="button" className="secondary" disabled={busy} onClick={async () => {
          await request(`/site/admin/skills/uploads/${uploadId}`, { method: "DELETE" }).catch(() => undefined);
          setPackages([]); setUploadId(""); setSelectedPackages([]);
        }}><X size={15} />取消</button>
          <button type="button" className="primary" disabled={busy || !selectedPackages.length} onClick={async () => {
            setBusy(true); setError("");
            try {
              const result = await request<{ items: { id: string; name: string }[] }>(
                `/site/admin/skills/uploads/${uploadId}/import`,
                json({ items: selectedPackages }),
              );
              setNotice(`已导入 ${result.items.length} 个 Skill`);
              router.replace("/admin/skills");
            } catch (reason) { setError(reason instanceof Error ? reason.message : "导入失败，请重试"); }
            finally { setBusy(false); }
          }}><Check size={15} />导入所选 {selectedPackages.length} 个 Skill</button>
        </footer>
      </section>}
      {replacing && <div className="admin-inline-confirm" role="alert"><span>导入会新建独立 Skill，当前草稿不会被修改。</span>
        <button className="secondary" disabled={busy} onClick={() => setReplacing(false)}>继续编辑</button>
        <button className="secondary" disabled={busy} onClick={() => { setReplacing(false); input.current?.click(); }}>选择 Skill 包</button>
      </div>}
      {conflict && <div className="admin-inline-confirm"><span>重新加载会放弃当前修改。</span><button className="secondary" onClick={() => {
        setConflict(false); setError(""); setDeleting(false); void resource.reload();
      }}>放弃修改并重新加载</button></div>}
      {(!id || version > 0) && <form className="provider-form model-detail-form skill-editor" onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset className="provider-form-fields" disabled={busy}>
          <div className="provider-form-grid">
            <label>名称<input required maxLength={64} value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
            <label className="provider-enabled"><input type="checkbox" checked={form.enabled} onChange={event => setForm({ ...form, enabled: event.target.checked })} />启用</label>
            <label className="provider-form-wide">用途描述<textarea required maxLength={1024} rows={2} value={form.description} onChange={event => setForm({ ...form, description: event.target.value })} /></label>
          </div>
          <Tabs.Root value={tab} onValueChange={setTab} className="skill-content">
            <Tabs.List className="skill-content-tabs" aria-label="指令内容"><Tabs.Trigger value="edit">编辑指令</Tabs.Trigger><Tabs.Trigger value="preview">预览</Tabs.Trigger><Tabs.Trigger value="files">补充资料{form.files.length ? ` (${form.files.length})` : ""}</Tabs.Trigger></Tabs.List>
            <Tabs.Content value="edit" forceMount className="skill-text-panel"><label>指令正文<textarea required rows={18} value={form.markdown} onInvalid={event => {
              if (tab !== "edit") { const textarea = event.currentTarget; event.preventDefault(); setTab("edit"); requestAnimationFrame(() => textarea.focus()); }
            }} onChange={event => setForm({ ...form, markdown: event.target.value })} /></label></Tabs.Content>
            <Tabs.Content value="preview"><div className="answer-markdown skill-preview"><ReactMarkdown skipHtml components={{
              img: ({ alt }) => <span>{alt || "图片"}</span>,
              a: ({ href, children }) => {
                if (!href || /^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith("//") || href.startsWith("#"))
                  return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
                let path = "";
                try { path = decodeURIComponent(href.split(/[?#]/)[0]).replace(/^(?:\.\/)+/, ""); } catch { /* Invalid links remain plain text. */ }
                if (path !== "SKILL.md" && !form.files.some(file => file.path === path)) return <span>{children}</span>;
                return <button className="skill-resource-link" type="button" onClick={() => {
                  if (path === "SKILL.md") setTab("edit");
                  else { setResourcePath(path); setTab("files"); }
                }}>{children}</button>;
              },
            }}>{form.markdown || "暂无内容"}</ReactMarkdown></div></Tabs.Content>
            <Tabs.Content value="files"><SkillResources files={form.files} path={resourcePath} onSelect={setResourcePath} review={packageReview} onRemove={path => setForm({ ...form, files: form.files.filter(file => file.path !== path) })} /></Tabs.Content>
          </Tabs.Root>
          <div className="skill-package-check">
            <button className="secondary" type="button" disabled={busy || !form.markdown.trim()} onClick={async () => {
              setBusy(true); setError("");
              try { const result = await request<PackageReview>("/site/admin/skills/review", json({ markdown: form.markdown, files: form.files })); setChecked(checkedPackage(form, result)); }
              catch (reason) { setError(reason instanceof Error ? reason.message : "检查失败，请重试"); }
              finally { setBusy(false); }
            }}>检查文件</button>
            {packageReview && (packageReview.can_enable ? <span role="status">文件检查通过，启用前请确认指令内容。</span>
              : <div role="alert"><strong>以下问题需处理后才能启用</strong><ul>{packageReview.issues.map(issue => <li key={issue}>{issue}</li>)}</ul></div>)}
          </div>
          <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span><div className="managed-actions">
            <button className="secondary" type="submit" disabled={busy || conflict || (!!id && !dirty)}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存</button>
            {!form.enabled && <button className="primary" type="submit" disabled={busy || conflict} onClick={event => {
              if (event.currentTarget.form?.reportValidity()) { event.preventDefault(); void save(true); }
            }}>保存并启用</button>}
          </div></div>
        </fieldset>
      </form>}
      {id && !conflict && <div className="managed-delete">
        {deleting ? <div className="admin-inline-confirm"><span>删除此 Skill？{dirty ? "当前修改将一并放弃。" : ""}</span>
          <button className="secondary" disabled={busy} onClick={() => setDeleting(false)}>取消</button>
          <button className="danger" disabled={busy} onClick={async () => {
            setBusy(true); setError("");
            try { await request(`/site/admin/skills/${id}`, json({ version }, "DELETE")); setSaved(form); router.replace("/admin/skills"); }
            catch (reason) { setError(reason instanceof Error ? reason.message : "删除失败"); setConflict(reason instanceof ApiError && reason.status === 409); }
            finally { setBusy(false); }
          }}>确认删除</button></div>
          : <button className="danger" disabled={busy} onClick={() => setDeleting(true)}><Trash2 size={16} />删除 Skill</button>}
      </div>}
    </AdminLoad>
  </>;
}
