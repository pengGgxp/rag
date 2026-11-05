"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, File, FileImage, FileText, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { API_URL, downloadFile, request, sessionHeaders } from "./api";
import { useAuth } from "./auth";
import { Modal } from "./modal";
import { attachmentAccept } from "./attachment-formats";
import { bytes, type StorageAllowances } from "./storage-types";
import { Tooltip } from "../components/ui/tooltip";
import { ResourceNotice } from "./resource-notice";

export type ResourceFile = { id: string; path: string; content_type: string; size_bytes: number;
  version: number; source: string; updated_at: string; availability?: "pending" | "missing" | "available" };
const sources: Record<string, string> = { chat_upload: "对话上传", resource_upload: "资源库上传", model_output: "模型生成" };

export function ResourceLibrary({ select }: { select?: (item: ResourceFile) => Promise<void> }) {
  const { session, role } = useAuth();
  const [items, setItems] = useState<ResourceFile[]>([]);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [quota, setQuota] = useState<StorageAllowances | null>(null);
  const [remove, setRemove] = useState<ResourceFile | null>(null);
  const [preview, setPreview] = useState<{ item: ResourceFile; url: string } | null>(null);
  const [textPreview, setTextPreview] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const canWrite = role === "admin" || role === "editor";

  async function load(signal?: AbortSignal) {
    setLoading(true);
    try {
      const data = await request<{ items: ResourceFile[]; has_more: boolean }>(
        `/resources?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&offset=${page * 50}&limit=50`, { signal });
      if (!signal?.aborted) { setItems(data.items); setMore(data.has_more); setError(""); }
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : "资源加载失败"); }
    finally { if (!signal?.aborted) setLoading(false); }
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [session.workspace_id, query, source, page]);
  useEffect(() => {
    const controller = new AbortController();
    setQuota(null);
    void request<StorageAllowances>("/storage/allowances", { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setQuota(value); }).catch(() => {});
    return () => controller.abort();
  }, [session.workspace_id]);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview.url); }, [preview]);

  async function upload(files: File[]) {
    if (!files.length || busy) return;
    setBusy(true); setError("");
    setTextPreview(null);
    try {
      for (const file of files) {
        const body = new FormData(); body.set("file", file);
        await request("/resources", { method: "POST", body });
      }
      await load();
      setQuota(await request<StorageAllowances>("/storage/allowances"));
    } catch (e) { await load(); setError(e instanceof Error ? e.message : "上传失败，请重试"); }
    finally { setBusy(false); }
  }
  async function open(item: ResourceFile) {
    setTextPreview(null);
    if (select) {
      setBusy(true); setError("");
      try { await select(item); } catch (e) { setError(e instanceof Error ? e.message : "选择失败，请重试"); }
      finally { setBusy(false); }
      return;
    }
    setBusy(true); setError("");
    try {
      if (!item.content_type.startsWith("image/") && item.content_type !== "application/pdf" && !item.content_type.startsWith("text/")) {
        await downloadFile(`/resources/${item.id}/download?version=${item.version}`, item.path.split("/").pop()!);
        return;
      }
      const response = await fetch(`${API_URL}/resources/${item.id}/download?version=${item.version}`, {
        credentials: "include", headers: sessionHeaders(),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(typeof data.detail === "string" ? data.detail : "预览失败，请重试或下载文件");
      }
      const blob = await response.blob();
      // Untrusted HTML/SVG is never embedded as active content.
      if (item.content_type.startsWith("text/") || item.content_type === "image/svg+xml") {
        setTextPreview(await blob.text());
        setPreview({ item, url: "" });
      } else setPreview({ item, url: URL.createObjectURL(blob) });
    } catch (e) { setError(e instanceof Error ? e.message : "预览失败"); }
    finally { setBusy(false); }
  }
  const due = quota?.cleanup_due_at ? new Date(quota.cleanup_due_at).toLocaleDateString("zh-CN") : "";
  const daysLeft = quota?.cleanup_due_at ? Math.max(0, Math.ceil((new Date(quota.cleanup_due_at).getTime() - Date.now()) / 86400000)) : null;
  return <div className={`workspace-files-content${select ? " resource-picker" : ""}`}>
    <div className="workspace-files-toolbar">
      <label className="workspace-files-search"><Search size={16} /><input aria-label="搜索资源" placeholder="搜索资源"
        value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} /></label>
      <select aria-label="上传来源" value={source} onChange={e => { setSource(e.target.value); setPage(0); }}>
        <option value="">全部来源</option>{Object.entries(sources).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      <Tooltip content="刷新"><button type="button" className="icon-button workspace-files-refresh" aria-label="刷新资源"
        disabled={loading || busy} onClick={() => void load()}><RefreshCw size={16} /></button></Tooltip>
      {!select && canWrite && <><input ref={input} type="file" accept={attachmentAccept} multiple hidden
        onChange={e => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        <button className="primary" disabled={busy || Boolean(quota?.over_quota_since)} onClick={() => input.current?.click()}><Upload size={16} />上传文件</button></>}
    </div>
    {!select && <ResourceNotice />}
    <div className="resource-list-summary"><span>{select ? "选择要添加到对话的文件" : "全部资源"}</span>{!select && quota && <span>存储已用 {bytes(quota.used_bytes)} / {bytes(quota.limit_bytes)}</span>}</div>
    {quota?.over_quota_since && <p className="auth-error" role="alert">空间已超额，暂停新增资源。请删除不需要的资源或增加额度；若持续超额，将于 {due} 起清理最早存入的资源。{daysLeft !== null && daysLeft <= 7 ? `宽限期${daysLeft <= 1 ? "即将结束" : `还剩 ${daysLeft} 天`}。` : ""}</p>}
    {error && <p className="workspace-files-error" role="alert">{error}</p>}
    <section className="workspace-files-results" aria-label="资源列表" aria-busy={loading || busy}>
      {loading ? <p className="workspace-files-state">正在加载资源…</p> : !items.length ? <div className="workspace-files-state"><FilesEmpty /><strong>{query || source ? "没有找到匹配的资源" : "暂无资源"}</strong><span>{query || source ? "试试其他关键词或来源" : "上传的附件和生成的文件会保存在这里"}</span></div> :
        <div className="workspace-file-list">{items.map(item => <div className="workspace-file-row" key={item.id}>
          <span className={`workspace-file-icon${item.content_type.startsWith("image/") ? " resource-icon-image" : ""}`}>{item.content_type.startsWith("image/") ? <FileImage size={21} /> : item.content_type.startsWith("text/") || item.content_type === "application/pdf" ? <FileText size={21} /> : <File size={21} />}</span><div className="workspace-file-copy">
            <button type="button" className="resource-file-name" disabled={busy || item.availability === "missing"} title={item.path} onClick={() => void open(item)}>{item.path.split("/").pop()}</button>
            <small>{bytes(item.size_bytes)} · {sources[item.source] ?? "资源"} · {item.availability === "missing" ? "文件已不可用" : item.availability === "pending" ? "待核验" : new Date(item.updated_at).toLocaleDateString("zh-CN")}</small>
          </div>
          {select && <ChevronRight size={16} className="resource-select-arrow" aria-hidden="true" />}
          {!select && <><Tooltip content="下载"><button type="button" className="icon-button workspace-file-download" aria-label={`下载 ${item.path}`} disabled={busy || item.availability === "missing"}
            onClick={() => void downloadFile(`/resources/${item.id}/download?version=${item.version}`, item.path.split("/").pop()!).catch(e => setError(e.message))}><Download size={16} /></button></Tooltip>
            {canWrite && <Tooltip content="删除"><button type="button" className="icon-button" aria-label={`删除 ${item.path}`} disabled={busy} onClick={() => setRemove(item)}><Trash2 size={16} /></button></Tooltip>}</>}
        </div>)}</div>}
    </section>
    {(page > 0 || more) && <div className="resource-pagination"><button className="secondary" disabled={page === 0 || loading} onClick={() => setPage(p => p - 1)}>上一页</button><span>{page + 1}</span><button className="secondary" disabled={!more || loading} onClick={() => setPage(p => p + 1)}>下一页</button></div>}
    {remove && <Modal title="删除资源" busy={busy} onClose={() => setRemove(null)}>
      <p className="resource-delete-copy">删除“{remove.path.split("/").pop()}”后，其他会话将无法继续读取此资源。</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="modal-footer"><button className="secondary" disabled={busy} onClick={() => setRemove(null)}>取消</button><button className="danger" disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try { await request(`/resources/${remove.id}`, { method: "DELETE" }); setRemove(null); await load(); }
        catch (e) { setError(e instanceof Error ? e.message : "删除失败"); }
        finally { setBusy(false); }
      }}>删除</button></div></Modal>}
    {preview && <Modal title={preview.item.path.split("/").pop()!} wide onClose={() => setPreview(null)}>
      {textPreview !== null ? <pre className="resource-text-preview">{textPreview}</pre> : preview.item.content_type.startsWith("image/") && preview.item.content_type !== "image/svg+xml" ?
        <img className="chat-image-preview" src={preview.url} alt={preview.item.path} /> :
        <iframe title={preview.item.path} className="resource-preview" sandbox="" src={preview.url} />}
    </Modal>}
  </div>;
}

function FilesEmpty() { return <File size={28} aria-hidden="true" />; }
