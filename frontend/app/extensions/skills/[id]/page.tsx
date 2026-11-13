"use client";
import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Download, Loader2, Save, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { ApiError, downloadFile, request } from "../../../api";
import { WorkspaceHeader, WorkspaceSkeleton } from "../../../workspace-header";
import { SkillImport } from "../../skill-import";
import { useExtensionResource } from "../../extension-data";

type SkillFile = { path: string; content: string };
type SkillDetail = { id: string; name: string; description: string; markdown?: string; files?: SkillFile[]; version?: number; directory?: string; installed?: boolean; installed_directory?: string };

export default function SkillPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const platform = typeof window !== "undefined" && new URLSearchParams(window.location.search).has("platform");
  const base = platform ? "/agent/skills/platform" : "/agent/skills";
  const resource = useExtensionResource<SkillDetail>(id === "new" ? null : `${base}/${id}`);
  const [content, setContent] = useState("");
  const [version, setVersion] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (resource.data?.markdown !== undefined) { setContent(resource.data.markdown); setVersion(resource.data.version ?? 1); }
  }, [resource.data]);

  async function save() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<SkillDetail>(`${base}/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, version }) });
      setContent(value.markdown ?? content); setVersion(value.version ?? version); setNotice("已保存，资源库和云电脑会读取同一份文件");
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? "文件已被其他入口修改，请重新加载后再保存" : cause instanceof Error ? cause.message : "保存失败，请重试"); }
    finally { setBusy(false); }
  }

  async function install() {
    setBusy(true); setError(""); setNotice("");
    try { const value = await request<{ directory: string; already_installed: boolean }>(`${base}/${id}/install`, { method: "POST" }); setNotice(value.already_installed ? `已安装在 ${value.directory}` : `已安装到 ${value.directory}`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "安装失败，请重试"); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError("");
    try { await request(`${base}/${id}`, { method: "DELETE" }); window.location.assign("/extensions"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "删除失败，请重试"); setBusy(false); }
  }

  if (id === "new") return <><WorkspaceHeader title="添加 Skill" /><div className="workspace-body extension-page"><Link className="extension-back" href="/extensions"><ArrowLeft size={16} />Skills</Link><SkillImport /></div></>;
  return <>
    <WorkspaceHeader title={resource.data?.name || (platform ? "平台 Skill" : "我的 Skill")} />
    <div className="workspace-body extension-page">
      <Link className="extension-back" href="/extensions"><ArrowLeft size={16} />Skills</Link>
      {resource.loading ? <WorkspaceSkeleton /> : resource.error ? <div className="extension-error" role="alert">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div> : resource.data && <>
        <section className="extension-section skill-detail-head"><div><h2>{resource.data.name}</h2><p>{resource.data.description}</p></div>
          <div className="extension-actions">{platform ? <><button className="primary" disabled={busy} onClick={() => void install()}>{busy ? <Loader2 size={16} className="spin" /> : "安装到当前工作区"}</button><button className="secondary" disabled={busy} onClick={() => void downloadFile(`${base}/${id}/download`, `${resource.data?.name || "skill"}.zip`)}><Download size={16} />下载包</button></> : <Link className="secondary" href="/resources">在资源库查看</Link>}</div>
        </section>
        {!platform ? <label className="extension-section">SKILL.md<textarea value={content} onChange={event => setContent(event.target.value)} rows={18} disabled={busy} /></label> : <div className="skill-preview extension-section"><ReactMarkdown skipHtml>{resource.data.markdown ?? ""}</ReactMarkdown></div>}
        {!!resource.data.files?.length && <section className="extension-section"><h3>包内文件</h3><ul>{resource.data.files.map(file => <li key={file.path}>{file.path}</li>)}</ul></section>}
        {error && <p className="extension-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
        {!platform && <div className="extension-actions"><button className="primary" disabled={busy || !content.trim()} onClick={() => void save()}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存</button>{!deleting ? <button className="danger" disabled={busy} onClick={() => setDeleting(true)}><Trash2 size={16} />删除 Skill</button> : <><span>删除整个 Skill 文件夹？资源库和云电脑将同时不可见。</span><button className="secondary" onClick={() => setDeleting(false)}>取消</button><button className="danger" disabled={busy} onClick={() => void remove()}>确认删除</button></>}</div>}
      </>}
    </div>
  </>;
}
