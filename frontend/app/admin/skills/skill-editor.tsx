"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import { ArrowLeft, Loader2, Save, Trash2, Upload } from "lucide-react";
import * as Tabs from "../../../components/ui/tabs";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import type { Skill } from "./page";
import "../../billing.css";
import "../platform-content.css";

type Draft = Pick<Skill, "name" | "description" | "markdown" | "enabled">;
const empty: Draft = { name: "", description: "", markdown: "", enabled: false };
const draftOf = ({ name, description, markdown, enabled }: Skill): Draft => ({ name, description, markdown, enabled });

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
  const [tab, setTab] = useState("edit");
  const input = useRef<HTMLInputElement>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  useAdminChanges(dirty, busy);
  function accept(value: Skill) { setForm(draftOf(value)); setSaved(draftOf(value)); setVersion(value.version); }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function save(enable?: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
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
      <input ref={input} type="file" accept=".md" hidden onChange={async event => {
        const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
        setBusy(true); setError("");
        try {
          const data = new FormData(); data.append("file", file);
          const value = await request<Draft>("/site/admin/skills/import", { method: "POST", body: data });
          setForm({ ...value, enabled: form.enabled }); setNotice("已导入，保存后生效");
        } catch (reason) { setError(reason instanceof Error ? reason.message : "导入失败，请重试"); }
        finally { setBusy(false); }
      }} />
      <button className="secondary" disabled={busy} onClick={() => input.current?.click()}><Upload size={16} />导入 Markdown</button>
    </AdminHeading>
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {error && <p className="admin-error" role="alert">{error}</p>}
      {notice && <p className="billing-notice" role="status">{notice}</p>}
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
            <Tabs.List className="skill-content-tabs" aria-label="指令内容"><Tabs.Trigger value="edit">编辑指令</Tabs.Trigger><Tabs.Trigger value="preview">预览</Tabs.Trigger></Tabs.List>
            <Tabs.Content value="edit" forceMount className="skill-text-panel"><label>指令正文<textarea required maxLength={50000} rows={18} value={form.markdown} onInvalid={event => {
              if (tab !== "edit") { const textarea = event.currentTarget; event.preventDefault(); setTab("edit"); requestAnimationFrame(() => textarea.focus()); }
            }} onChange={event => setForm({ ...form, markdown: event.target.value })} /></label></Tabs.Content>
            <Tabs.Content value="preview"><div className="answer-markdown skill-preview"><ReactMarkdown skipHtml components={{ img: ({ alt }) => <span>{alt || "图片"}</span> }}>{form.markdown || "暂无内容"}</ReactMarkdown></div></Tabs.Content>
          </Tabs.Root>
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
