"use client";
import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Check, ChevronRight, Download, FileCode2, FileText, FolderOpen, Loader2, Pencil, Save, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { ApiError, downloadFile, request } from "../../../api";
import { WorkspaceHeader, WorkspaceSkeleton } from "../../../workspace-header";
import { SkillImport } from "../../skill-import";
import { useExtensionResource } from "../../extension-data";
import { useAuth } from "../../../auth";
import { useAdminChanges } from "../../../admin/admin-changes";
import { Modal } from "../../../modal";
import { ResourcePreview } from "../../../resource-preview";
import type { ResourceFile } from "../../../resource-library";
import { bytes } from "../../../storage-types";
import { SkillResources } from "../../../admin/skills/skill-resources";
import type { PackageReview } from "../../../admin/skills/page";

type SkillFile = { path: string; content: string };
type SkillDetail = { id: string; name: string | null; description: string; markdown?: string; files?: SkillFile[]; review?: PackageReview; revision?: string; directory?: string; manifest_path?: string; installed?: boolean; installed_directory?: string; installed_directories?: string[]; manifest_error?: string };

export default function SkillPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { role, session } = useAuth();
  const canWrite = role === "admin" || role === "editor";
  const paramsSearch = useSearchParams();
  const platform = paramsSearch.has("platform");
  const requestedReturn = paramsSearch.get("returnTo");
  const returnTo = requestedReturn && /^\/extensions(?:\?|$)/.test(requestedReturn) ? requestedReturn : "/extensions";
  const [editing, setEditing] = useState(paramsSearch.get("action") === "edit");
  const handledAction = useRef("");
  const base = platform ? "/agent/skills/platform" : "/agent/skills";
  const resource = useExtensionResource<SkillDetail>(id === "new" ? null : `${base}/${id}`);
  const files = useExtensionResource<ResourceFile[]>(platform || id === "new" ? null : `${base}/${id}/files`);
  const [content, setContent] = useState("");
  const [revision, setRevision] = useState<string | undefined>();
  const [initialContent, setInitialContent] = useState("");
  const [deletePreview, setDeletePreview] = useState<{ files: number; directories: number; confirmation: string } | null>(null);
  const [installedPaths, setInstalledPaths] = useState<string[] | null>(null);
  const [choosingDirectory, setChoosingDirectory] = useState(false);
  const [packagePath, setPackagePath] = useState<string | null>(null);
  const [packageOrigin, setPackageOrigin] = useState("");
  const [preview, setPreview] = useState<ResourceFile | null>(null);
  const [discard, setDiscard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleting, setDeleting] = useState(false);
  const dirty = !platform && content !== initialContent;
  useAdminChanges(dirty, busy);
  const directories = installedPaths ?? resource.data?.installed_directories ?? (resource.data?.installed_directory ? [resource.data.installed_directory] : []);
  const workspaceUrl = (directory: string) => `/resources?path=${encodeURIComponent(directory)}`;
  const packageFiles = resource.data?.files ?? [];
  const workspaceFiles = files.data?.filter(file => file.path !== resource.data?.manifest_path) ?? [];

  useEffect(() => {
    if (resource.data?.markdown !== undefined) { setContent(resource.data.markdown); setInitialContent(resource.data.markdown); setRevision(resource.data.revision); }
  }, [resource.data]);
  useEffect(() => { setInstalledPaths(null); setPackagePath(null); setPreview(null); setNotice(""); setError(""); setEditing(paramsSearch.get("action") === "edit"); }, [id, platform, paramsSearch]);
  useEffect(() => {
    if (!resource.data || platform || !canWrite || paramsSearch.get("action") !== "uninstall" || handledAction.current === id) return;
    handledAction.current = id;
    void beginDelete();
  }, [resource.data, id, platform, canWrite, paramsSearch]);

  async function save() {
    if (!dirty || busy || !canWrite) return;
    setBusy(true); setSaving(true); setError(""); setNotice("");
    try {
      const value = await request<SkillDetail>(`${base}/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, revision, request_id: crypto.randomUUID() }) });
      setContent(value.markdown ?? content); setInitialContent(value.markdown ?? content); setRevision(value.revision); setNotice("已保存");
      await resource.reload();
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? "文件已被其他入口修改，请重新加载后再保存" : cause instanceof Error ? cause.message : "保存失败，请重试"); }
    finally { setBusy(false); setSaving(false); }
  }

  async function install() {
    setBusy(true); setError(""); setNotice("");
    try { const value = await request<{ directory: string; already_installed: boolean }>(`${base}/${id}/install`, { method: "POST", headers: { "X-Request-ID": crypto.randomUUID() } }); setInstalledPaths([value.directory]); setNotice(value.already_installed ? `工作区已有：${value.directory}` : `已安装到 ${value.directory}`); await resource.reload(); setInstalledPaths(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "安装失败，请重试"); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError("");
    try { await request(`${base}/${id}?confirmation=${encodeURIComponent(deletePreview?.confirmation || "")}&request_id=${crypto.randomUUID()}`, { method: "DELETE" }); setInitialContent(content); router.push(returnTo); }
    catch (cause) { const detail = cause instanceof ApiError ? cause.detail as { preview?: typeof deletePreview } | undefined : undefined; if (detail?.preview) setDeletePreview(detail.preview); setError(cause instanceof Error ? cause.message : "删除失败，请重试"); setBusy(false); }
  }

  async function download() {
    setDownloading(true); setError("");
    try { await downloadFile(`${base}/${id}/download`, `${resource.data?.name || "skill"}.zip`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "下载失败，请重试"); }
    finally { setDownloading(false); }
  }

  async function beginDelete() {
    setBusy(true); setError("");
    try { const value = await request<{ files: number; directories: number; confirmation: string }>("/resources/operations/delete-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [resource.data!.directory] }) }); setDeletePreview(value); setDeleting(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "无法确认删除范围，请重试"); }
    finally { setBusy(false); }
  }

  const feedback = <>{error && <div className="skill-detail-feedback extension-error" role="alert"><span>{error}</span>{!platform && <button id="skill-reload" className="secondary" disabled={busy} onClick={() => dirty ? setDiscard(true) : void resource.reload()}>重新加载</button>}</div>}{platform && notice && <p className="skill-detail-feedback" role="status">{notice}</p>}</>;

  if (id === "new") return <><WorkspaceHeader title="添加 Skill" /><div className="workspace-body extension-page"><div className="skill-detail-content"><Link className="extension-back" href={returnTo}><ArrowLeft size={16} />返回 Skill 仓库</Link><SkillImport /></div></div></>;
  return <>
    <WorkspaceHeader title={resource.data?.name || (resource.data ? "待修复的 Skill" : "Skill 详情")} />
    <div className="workspace-body extension-page skill-detail-page">
      <div className="skill-detail-content">
        <Link className="extension-back" href={returnTo}><ArrowLeft size={16} />返回 Skill 仓库</Link>
        {resource.loading && !resource.data ? <WorkspaceSkeleton /> : resource.error ? <div className="extension-error" role="alert">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div> : resource.data && <>
          <section className="skill-detail-overview" aria-label="Skill 概要">
            <div className="skill-detail-status"><span className="skill-detail-source">{!platform || directories.length ? <><Check size={14} />已安装</> : "未安装"}</span>{!canWrite && <span>只读</span>}</div>
            {resource.data.description && <p className="skill-detail-description">{resource.data.description}</p>}
            {(!platform || directories.length > 0) && <p className="skill-detail-location"><FolderOpen size={15} /><span>{!platform ? resource.data.directory : directories.length === 1 ? directories[0] : `${directories.length} 个同名目录，查看时选择位置`}</span></p>}
            <div className="extension-actions">
              {!platform && canWrite && <button className={editing ? "secondary" : "primary"} disabled={busy || (editing && dirty)} onClick={() => setEditing(value => !value)}><Pencil size={16} />{editing ? "查看说明" : "编辑 Skill"}</button>}
              {platform && !directories.length && <button className="primary" disabled={busy || !canWrite} onClick={() => void install()}>{busy ? <Loader2 size={16} className="spin" /> : <Download size={16} />}安装到当前工作区</button>}
              {(!platform || directories.length > 0) && (platform && directories.length > 1
                ? <button id="skill-choose-directory" className="primary" aria-haspopup="dialog" onClick={() => setChoosingDirectory(true)}><FolderOpen size={16} />在工作区查看</button>
                : <Link className={platform ? "primary" : "secondary"} href={workspaceUrl(platform ? directories[0] : resource.data.directory || "")}><FolderOpen size={16} />在工作区查看</Link>)}
              {platform && <button className="secondary" disabled={downloading} onClick={() => void download()}>{downloading ? <Loader2 size={16} className="spin" /> : <Download size={16} />}{downloading ? "正在下载" : "下载包"}</button>}
            </div>
            {platform && !directories.length && !canWrite && <p className="skill-detail-muted">需要工作区编辑权限才能安装，可先查看说明或下载包。</p>}
            {platform && feedback}
            {!platform && !editing && feedback}
          </section>
          {resource.data.manifest_error && <p className="skill-detail-feedback extension-error" role="alert">{resource.data.manifest_error}</p>}
          {platform || !editing ? <section className="skill-detail-section" aria-labelledby="skill-instructions-title">
            <h2 id="skill-instructions-title">使用说明</h2>
            <div className="answer-markdown skill-detail-markdown"><ReactMarkdown skipHtml components={{
              img: ({ alt }) => <span>{alt || "包内图片"}</span>,
              a: ({ href, children }) => {
                if (href && /^https?:\/\//i.test(href)) return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
                let path = "";
                try { path = decodeURIComponent((href || "").split(/[?#]/)[0]).replace(/^(?:\.\/)+/, ""); } catch { /* Invalid references remain readable. */ }
                const workspaceFile = workspaceFiles.find(file => file.path === `${resource.data!.directory}/${path}`);
                return (platform ? packageFiles.some(file => file.path === path) : workspaceFile) ? <button className="skill-resource-link" data-skill-reference={encodeURIComponent(path)} onClick={() => { if (!platform && workspaceFile) setPreview(workspaceFile); else { setPackageOrigin(`[data-skill-reference="${encodeURIComponent(path)}"]`); setPackagePath(path); } }}>{children}</button> : <span>{children}</span>;
              },
            }}>{(resource.data.markdown || "暂无使用说明").replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "")}</ReactMarkdown></div>
          </section> : resource.data.markdown !== undefined && <section className="skill-detail-section skill-detail-editor" aria-labelledby="skill-editor-title">
            <div className="skill-detail-section-heading"><h2 id="skill-editor-title"><label htmlFor="skill-content">SKILL.md</label></h2><span className="skill-detail-muted">{canWrite ? "编辑文件" : "只读文件"}</span></div>
            <p className="skill-detail-file-path">{resource.data.manifest_path}</p>
            <textarea id="skill-content" aria-label="Skill 内容" spellCheck={false} value={content} onChange={event => { setContent(event.target.value); setNotice(""); }} rows={14} readOnly={!canWrite} disabled={busy} />
            {!platform && feedback}
            <div className="skill-detail-savebar"><span role="status" aria-live="polite">{saving ? "正在保存…" : dirty ? "有未保存的修改" : notice || (canWrite ? "没有未保存的修改" : "当前内容为只读")}</span>{canWrite && <button className="primary" disabled={busy || !dirty || !content.trim()} onClick={() => void save()}>{saving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存</button>}</div>
          </section>}
          <section className="skill-detail-section" aria-labelledby="skill-files-title">
            <div className="skill-detail-section-heading"><h2 id="skill-files-title">包内文件</h2>{(platform || (!files.loading && !files.error)) && <span className="skill-detail-muted">{platform ? packageFiles.length : workspaceFiles.length} 个附属文件</span>}</div>
            {!platform && files.loading ? <WorkspaceSkeleton /> : !platform && files.error ? <div className="extension-error" role="alert">{files.error}<button className="secondary" onClick={() => void files.reload()}>重新加载文件</button></div> : <ul className="skill-detail-files">
              {(platform ? packageFiles : workspaceFiles).map(file => {
                const path = platform ? file.path : file.path.slice((resource.data!.directory?.length || 0) + 1);
                return <li key={file.path}><button type="button" data-skill-file={encodeURIComponent(file.path)} data-resource-id={"id" in file ? String(file.id) : undefined} aria-label={`查看 ${path}`} onClick={() => { if (platform) { setPackageOrigin(`[data-skill-file="${encodeURIComponent(file.path)}"]`); setPackagePath(file.path); } else setPreview(file as ResourceFile); }}>
                  {/\.(py|js|ts|sh)$/i.test(path) ? <FileCode2 size={19} /> : <FileText size={19} />}<span className="skill-detail-file-copy"><strong>{path.split("/").pop()}</strong><small>{path}</small></span>{"size_bytes" in file && <span className="skill-detail-muted">{bytes(file.size_bytes)}</span>}<ChevronRight size={16} />
                </button></li>;
              })}
              {!(platform ? packageFiles : workspaceFiles).length && <li className="skill-detail-muted skill-detail-no-files">没有附属文件</li>}
            </ul>}
          </section>
          {!platform && canWrite && <section className="skill-detail-danger" aria-label="卸载 Skill"><div><h2>卸载 Skill</h2><p>移除此安装目录及其中的文件，影响当前工作区的所有成员。</p></div><button id="skill-delete" className="danger" disabled={busy} onClick={() => void beginDelete()}><Trash2 size={16} />卸载</button></section>}
          {deleting && <Modal title="卸载 Skill" restoreFocusSelector="#skill-delete" busy={busy} onClose={() => setDeleting(false)}><div className="skill-detail-confirm"><p>{resource.data.directory}</p><p>将删除 {deletePreview?.files} 个文件和 {deletePreview?.directories} 个文件夹，影响当前工作区的所有成员。{dirty && "未保存的修改也会被放弃。"}</p>{error && <p className="extension-error" role="alert">{error}</p>}<div className="extension-actions"><button className="secondary" disabled={busy} onClick={() => setDeleting(false)}>取消</button><button className="danger" disabled={busy} onClick={() => void remove()}>确认卸载</button></div></div></Modal>}
          {discard && <Modal title="重新加载文件" restoreFocusSelector="#skill-reload, #skill-content" onClose={() => setDiscard(false)}><div className="skill-detail-confirm"><p>放弃尚未保存的修改并重新加载？</p><div className="extension-actions"><button className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button className="danger" onClick={() => { setDiscard(false); setError(""); setNotice(""); void resource.reload(); }}>放弃修改</button></div></div></Modal>}
          {choosingDirectory && <Modal title="选择工作区目录" restoreFocusSelector="#skill-choose-directory" onClose={() => setChoosingDirectory(false)}><div className="skill-detail-confirm"><p>当前工作区有 {directories.length} 个同名 Skill，请选择要查看的位置。</p><ul className="skill-detail-directory-list">{directories.map(directory => <li key={directory}><Link href={workspaceUrl(directory)}><FolderOpen size={18} /><span>{directory}</span><ChevronRight size={16} /></Link></li>)}</ul></div></Modal>}
          {packagePath && <Modal title="包内文件" wide restoreFocusSelector={packageOrigin} onClose={() => setPackagePath(null)}><div className="skill-detail-package-preview"><SkillResources files={packageFiles} review={resource.data.review} path={packagePath} onSelect={setPackagePath} personal /></div></Modal>}
          {preview && <ResourcePreview item={preview} scope={`${session.user.id}:${session.workspace_id}`} onClose={() => setPreview(null)} />}
        </>}
      </div>
    </div>
  </>;
}
