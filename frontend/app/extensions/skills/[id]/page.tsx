"use client";
import { use, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Download, Loader2, Save, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { ApiError, downloadFile, request } from "../../../api";
import { WorkspaceHeader, WorkspaceSkeleton } from "../../../workspace-header";
import { SkillImport } from "../../skill-import";
import { useExtensionResource } from "../../extension-data";
import { useAuth } from "../../../auth";
import { useAdminChanges } from "../../../admin/admin-changes";

type SkillFile = { path: string; content: string };
type SkillDetail = { id: string; name: string; description: string; markdown?: string; files?: SkillFile[]; version?: number; revision?: string; directory?: string; installed?: boolean; installed_directory?: string; manifest_error?: string };

export default function SkillPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { role } = useAuth();
  const canWrite = role === "admin" || role === "editor";
  const platform = useSearchParams().has("platform");
  const base = platform ? "/agent/skills/platform" : "/agent/skills";
  const resource = useExtensionResource<SkillDetail>(id === "new" ? null : `${base}/${id}`);
  const [content, setContent] = useState("");
  const [version, setVersion] = useState(1);
  const [revision, setRevision] = useState<string | undefined>();
  const [initialContent, setInitialContent] = useState("");
  const [deletePreview, setDeletePreview] = useState<{ files: number; directories: number; confirmation: string } | null>(null);
  const [installedDirectory, setInstalledDirectory] = useState<string | null>(null);
  const [discard, setDiscard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleting, setDeleting] = useState(false);
  useAdminChanges(!platform && content !== initialContent, busy);

  useEffect(() => {
    if (resource.data?.markdown !== undefined) { setContent(resource.data.markdown); setInitialContent(resource.data.markdown); setVersion(resource.data.version ?? 1); setRevision(resource.data.revision); }
  }, [resource.data]);

  async function save() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<SkillDetail>(`${base}/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, revision, request_id: crypto.randomUUID() }) });
      setContent(value.markdown ?? content); setInitialContent(value.markdown ?? content); setVersion(value.version ?? version); setRevision(value.revision); setNotice("已保存");
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? "文件已被其他入口修改，请重新加载后再保存" : cause instanceof Error ? cause.message : "保存失败，请重试"); }
    finally { setBusy(false); }
  }

  async function install() {
    setBusy(true); setError(""); setNotice("");
    try { const value = await request<{ directory: string; already_installed: boolean }>(`${base}/${id}/install`, { method: "POST", headers: { "X-Request-ID": crypto.randomUUID() } }); setInstalledDirectory(value.directory); setNotice(value.already_installed ? `工作区已有：${value.directory}` : `已安装到 ${value.directory}`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "安装失败，请重试"); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError("");
    try { await request(`${base}/${id}?confirmation=${encodeURIComponent(deletePreview?.confirmation || "")}&request_id=${crypto.randomUUID()}`, { method: "DELETE" }); setInitialContent(content); router.push("/extensions"); }
    catch (cause) { const detail = cause instanceof ApiError ? cause.detail as { preview?: typeof deletePreview } | undefined : undefined; if (detail?.preview) setDeletePreview(detail.preview); setError(cause instanceof Error ? cause.message : "删除失败，请重试"); setBusy(false); }
  }

  if (id === "new") return <><WorkspaceHeader title="添加 Skill" /><div className="workspace-body extension-page"><Link className="extension-back" href="/extensions"><ArrowLeft size={16} />Skills</Link><SkillImport /></div></>;
  return <>
    <WorkspaceHeader title={resource.data?.name || (platform ? "平台 Skill" : "我的 Skill")} />
    <div className="workspace-body extension-page">
      <Link className="extension-back" href="/extensions"><ArrowLeft size={16} />Skills</Link>
      {resource.loading ? <WorkspaceSkeleton /> : resource.error ? <div className="extension-error" role="alert">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div> : resource.data && <>
        <section className="extension-section skill-detail-head"><div><h2>{resource.data.name}</h2><p>{resource.data.description}</p></div>
          <div className="extension-actions">{platform ? <><button className="primary" disabled={busy || !canWrite} onClick={() => void install()}>{busy ? <Loader2 size={16} className="spin" /> : "安装到当前工作区"}</button><button className="secondary" disabled={busy} onClick={() => void downloadFile(`${base}/${id}/download`, `${resource.data?.name || "skill"}.zip`).catch(cause => setError(String(cause)))}><Download size={16} />下载包</button>{(installedDirectory || resource.data.installed_directory) && <Link className="secondary" href={`/resources?path=${encodeURIComponent(installedDirectory || resource.data.installed_directory || "")}`}>在工作区查看</Link>}</> : <Link className="secondary" href={`/resources?path=${encodeURIComponent(resource.data.directory || "")}`}>在工作区查看</Link>}</div>
        </section>
        {resource.data.manifest_error && <p className="extension-error" role="alert">{resource.data.manifest_error}</p>}
        {!platform ? resource.data.markdown !== undefined && <label className="extension-section">SKILL.md<textarea aria-label="Skill 内容" value={content} onChange={event => setContent(event.target.value)} rows={18} readOnly={!canWrite} disabled={busy} /></label> : <div className="skill-preview extension-section"><ReactMarkdown skipHtml>{resource.data.markdown ?? ""}</ReactMarkdown></div>}
        {!!resource.data.files?.length && <section className="extension-section"><h3>包内文件</h3><ul>{resource.data.files.map(file => <li key={file.path}>{file.path}</li>)}</ul></section>}
        {error && <p className="extension-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
        {error && !platform && <button className="secondary" disabled={busy} onClick={() => setDiscard(true)}>重新加载</button>}
        {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的修改并重新加载？</span><button className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button className="danger" onClick={() => { setDiscard(false); void resource.reload(); }}>放弃修改</button></div>}
        {!platform && canWrite && <div className="extension-actions"><button className="primary" disabled={busy || !content.trim()} onClick={() => void save()}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存</button>{!deleting ? <button className="danger" disabled={busy} onClick={async () => { setBusy(true); try { const preview = await request<{ files: number; directories: number; confirmation: string }>("/resources/operations/delete-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [resource.data!.directory] }) }); setDeletePreview(preview); setDeleting(true); } catch (cause) { setError(String(cause)); } finally { setBusy(false); } }}><Trash2 size={16} />删除 Skill</button> : <><span>删除 {deletePreview?.files} 个文件和 {deletePreview?.directories} 个文件夹？这会影响同一工作区的所有成员。</span><button className="secondary" onClick={() => setDeleting(false)}>取消</button><button className="danger" disabled={busy} onClick={() => void remove()}>确认删除</button></>}</div>}
      </>}
    </div>
  </>;
}
