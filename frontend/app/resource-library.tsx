"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, FolderOpen, LayoutGrid, List, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { downloadFile, request } from "./api";
import { useAuth } from "./auth";
import { Modal } from "./modal";
import { attachmentAccept } from "./attachment-formats";
import { bytes, type StorageAllowances } from "./storage-types";
import { Tooltip } from "../components/ui/tooltip";
import { ResourceNotice } from "./resource-notice";
import { ResourcePreview, ResourceThumbnail } from "./resource-preview";
import "./resource-library.css";

export type ResourceFile = { id: string; path: string; content_type: string; size_bytes: number;
  source: string; updated_at: string; availability?: "pending" | "missing" | "available" };
const sources: Record<string, string> = { chat_upload: "对话上传", resource_upload: "资源库上传", model_output: "模型生成", platform: "平台 Skill" };

export function ResourceLibrary({ select }: { select?: (item: ResourceFile) => Promise<void> }) {
  const { session, role } = useAuth();
  const [items, setItems] = useState<ResourceFile[]>([]), [query, setQuery] = useState(""), [source, setSource] = useState("");
  const [page, setPage] = useState(0), [more, setMore] = useState(false), [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [quota, setQuota] = useState<StorageAllowances | null>(null);
  const [remove, setRemove] = useState<ResourceFile | null>(null), [preview, setPreview] = useState<ResourceFile | null>(null);
  const [view, setView] = useState<"grid" | "list">("grid");
  const input = useRef<HTMLInputElement>(null), results = useRef<HTMLElement>(null);
  const canWrite = role === "admin" || role === "editor", scope = `${session.user.id}:${session.workspace_id}`;
  useEffect(() => {
    try { setView(localStorage.getItem(`resource-view:${session.user.id}`) === "list" ? "list" : "grid"); } catch { /* Optional preference. */ }
  }, [session.user.id]);
  useEffect(() => { setPreview(null); setPage(0); setItems([]); }, [scope]);
  useEffect(() => {
    const root = results.current;
    if (!root || select || view !== "grid") return;
    const measure = (entry: Element) => {
      const element = entry as HTMLElement;
      element.style.gridRowEnd = `span ${Math.ceil((element.getBoundingClientRect().height + 20) / 8)}`;
    };
    const observer = new ResizeObserver(entries => entries.forEach(entry => measure(entry.target)));
    root.querySelectorAll(".resource-card").forEach(element => { measure(element); observer.observe(element); });
    return () => observer.disconnect();
  }, [items, view, loading, select]);
  async function load(signal?: AbortSignal) {
    setLoading(true);
    try {
      const data = await request<{ items: ResourceFile[]; has_more: boolean }>(`/resources?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&offset=${page * 40}&limit=40`, { signal });
      if (!signal?.aborted) { setItems(data.items); setMore(data.has_more); setError(""); }
    } catch (cause) { if (!signal?.aborted) setError(cause instanceof Error ? cause.message : "资源加载失败"); }
    finally { if (!signal?.aborted) setLoading(false); }
  }
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => void load(controller.signal), query ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [scope, query, source, page]);
  useEffect(() => {
    const controller = new AbortController(); setQuota(null);
    void request<StorageAllowances>("/storage/allowances", { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setQuota(value); }).catch(() => {});
    return () => controller.abort();
  }, [scope]);
  async function upload(files: File[]) {
    if (!files.length || busy) return;
    setBusy(true); setError("");
    try {
      for (const file of files) { const body = new FormData(); body.set("file", file); await request("/resources", { method: "POST", body }); }
      await load(); setQuota(await request<StorageAllowances>("/storage/allowances"));
    } catch (cause) { await load(); setError(cause instanceof Error ? cause.message : "上传失败，请重试"); }
    finally { setBusy(false); }
  }
  async function open(item: ResourceFile) {
    if (!select) { setPreview(item); return; }
    setBusy(true); setError("");
    try { await select(item); } catch (cause) { setError(cause instanceof Error ? cause.message : "选择失败，请重试"); }
    finally { setBusy(false); }
  }
  const grid = !select && view === "grid";
  return <div className={`resource-library ${select ? "resource-picker-v2" : ""}`}>
    <div className="resource-browser-toolbar">
      <label className="resource-search"><Search size={17} /><input aria-label="搜索资源" placeholder="搜索文件…" value={query} onChange={event => { setQuery(event.target.value); setPage(0); }} /></label>
      <select aria-label="上传来源" value={source} onChange={event => { setSource(event.target.value); setPage(0); }}><option value="">全部来源</option>{Object.entries(sources).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
      <div className="resource-toolbar-tail">
        {!select && <div className="resource-view-switch" role="group" aria-label="浏览方式">{(["grid", "list"] as const).map(mode => <Tooltip content={mode === "grid" ? "瀑布流" : "列表"} key={mode}><button type="button" aria-label={mode === "grid" ? "瀑布流" : "列表"} aria-pressed={view === mode} onClick={() => { setView(mode); try { localStorage.setItem(`resource-view:${session.user.id}`, mode); } catch {} }}>{mode === "grid" ? <LayoutGrid size={17} /> : <List size={18} />}</button></Tooltip>)}</div>}
        <Tooltip content="刷新"><button type="button" className="icon-button" aria-label="刷新资源" disabled={loading || busy} onClick={() => void load()}><RefreshCw size={17} /></button></Tooltip>
        {!select && canWrite && <><input ref={input} type="file" accept={attachmentAccept} multiple hidden onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ""; }} /><button type="button" className="primary" disabled={busy || Boolean(quota?.over_quota_since)} onClick={() => input.current?.click()}><Upload size={16} />{busy ? "正在上传" : "上传文件"}</button></>}
      </div>
    </div>
    {!select && <ResourceNotice />}
    <div className="resource-browser-summary"><span>{select ? "选择文件" : source ? sources[source] : "全部资源"}{items.length > 0 && <small>{more ? `${items.length}+` : items.length}</small>}</span>{!select && quota && <span>已用 {bytes(quota.used_bytes)}<span className="resource-summary-divider">/</span>{bytes(quota.limit_bytes)}</span>}</div>
    {quota?.over_quota_since && <p className="resource-browser-alert" role="alert">空间已超额，请删除不需要的资源或增加额度。若持续超额，将于 {quota.cleanup_due_at ? new Date(quota.cleanup_due_at).toLocaleDateString("zh-CN") : "宽限期结束后"} 起清理最早存入的资源。</p>}
    {error && <p className="resource-browser-alert" role="alert">{error}</p>}
    <section ref={results} className={`resource-browser-results ${grid ? "resource-masonry" : "resource-rows"}`} aria-label="资源列表" aria-busy={loading || busy}>
      {loading ? Array.from({ length: grid ? 8 : 4 }, (_, index) => <div key={index} className={`resource-skeleton ${grid ? "" : "resource-skeleton-row"}`} aria-hidden="true" />)
        : !items.length ? <div className="resource-browser-empty"><FolderOpen size={40} strokeWidth={1.15} /><h2>{query || source ? "没有找到资源" : "你的资源，从这里开始"}</h2><p>{query || source ? "试试其他关键词或来源" : "上传文件，或在对话中创建新的成果"}</p>{canWrite && !select && !query && !source && <button type="button" className="secondary" onClick={() => input.current?.click()}><Upload size={16} />上传文件</button>}</div>
        : items.map(item => <article key={`${scope}:${item.id}`} className={`resource-item ${grid ? "resource-card" : "resource-row"}`}>
          <button type="button" className="resource-item-open" data-resource-id={item.id} disabled={busy || item.availability === "missing"} onClick={() => void open(item)} aria-label={`${select ? "选择" : "预览"} ${item.path}`}>
            <ResourceThumbnail item={item} scope={scope} compact={!grid} />
            <div className="resource-item-copy"><span className="resource-item-name" title={item.path}>{item.path.split("/").pop()}</span><span className="resource-item-meta">{item.availability === "missing" ? "文件已不可用" : `${bytes(item.size_bytes)} · ${sources[item.source] ?? "资源"}`}</span></div>
          </button>
          {select ? <ChevronRight size={16} className="resource-row-chevron" /> : <div className="resource-item-bottom"><time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" })}</time><div className="resource-item-actions"><Tooltip content="下载"><button type="button" className="icon-button" aria-label={`下载 ${item.path}`} disabled={item.availability === "missing"} onClick={() => void downloadFile(`/resources/${item.id}/download`, item.path.split("/").pop()!).catch(cause => setError(cause.message))}><Download size={15} /></button></Tooltip>{canWrite && <Tooltip content="删除"><button type="button" className="icon-button" aria-label={`删除 ${item.path}`} onClick={() => setRemove(item)}><Trash2 size={15} /></button></Tooltip>}</div></div>}
        </article>)}
    </section>
    {(page > 0 || more) && <div className="resource-browser-pagination"><button className="secondary" disabled={!page || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft size={16} />上一页</button><span>{page + 1}</span><button className="secondary" disabled={!more || loading} onClick={() => setPage(value => value + 1)}>下一页<ChevronRight size={16} /></button></div>}
    {preview && <ResourcePreview key={`${scope}:${preview.id}`} item={preview} scope={scope} onClose={() => setPreview(null)} />}
    {remove && <Modal title="删除资源" busy={busy} onClose={() => setRemove(null)}><p className="resource-delete-copy">删除“{remove.path.split("/").pop()}”后，其他会话将无法继续读取此资源。</p>{error && <p role="alert" className="form-error">{error}</p>}<div className="modal-footer"><button type="button" className="secondary" onClick={() => setRemove(null)} disabled={busy}>取消</button><button type="button" className="danger" disabled={busy} onClick={async () => {
      setBusy(true); setError("");
      try { await request(`/resources/${remove.id}`, { method: "DELETE" }); setRemove(null); await load(); }
      catch (cause) { setError(cause instanceof Error ? cause.message : "删除失败"); }
      finally { setBusy(false); }
    }}>删除</button></div></Modal>}
  </div>;
}
