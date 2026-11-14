"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, Check, ChevronRight, Clipboard, Copy, Download, Edit3, Folder, FolderOpen, LayoutGrid, List, MoreVertical, Pencil, Plus, RefreshCw, Scissors, Search, Trash2, Upload, X } from "lucide-react";
import { downloadFile, request } from "./api";
import { useAuth } from "./auth";
import { Modal } from "./modal";
import { attachmentAccept } from "./attachment-formats";
import { bytes, type StorageAllowances } from "./storage-types";
import { Tooltip } from "../components/ui/tooltip";
import { ResourceNotice } from "./resource-notice";
import { ResourcePreview, ResourceThumbnail } from "./resource-preview";
import "./resource-library.css";

export type ResourceFile = { id: string; path: string; content_type: string; size_bytes: number; source: string; updated_at: string; availability?: "pending" | "missing" | "available" };
type Item = (ResourceFile & { kind: "file"; name: string }) | { kind: "directory"; name: string; path: string; updated_at?: string; size_bytes?: number; content_type?: string };
const sources: Record<string, string> = { chat_upload: "对话上传", resource_upload: "工作区上传", model_output: "模型生成", platform: "平台 Skill" };
function itemType(item: Item) {
  if (item.kind === "directory") return "文件夹";
  const extension = item.name.includes(".") ? item.name.split(".").pop()?.toLowerCase() : "";
  return ({ md: "Markdown", txt: "文本文档", pdf: "PDF 文档", doc: "Word 文档", docx: "Word 文档", xlsx: "Excel 表格", csv: "CSV 表格", py: "Python 脚本", js: "JavaScript", ts: "TypeScript", zip: "压缩包" } as Record<string, string>)[extension || ""] || (extension ? `${extension.toUpperCase()} 文件` : "文件");
}

export function ResourceLibrary({ select }: { select?: (item: ResourceFile) => Promise<void> }) {
  const { session, role } = useAuth();
  const [items, setItems] = useState<Item[]>([]), [path, setPath] = useState(""), [query, setQuery] = useState(""), [source, setSource] = useState(""), [sort, setSort] = useState<"name" | "type" | "updated_at">("name");
  const [crumbs, setCrumbs] = useState<{ name: string; path: string }[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid"), [selected, setSelected] = useState<Set<string>>(new Set()), [menu, setMenu] = useState<{ x: number; y: number; item: Item } | null>(null);
  const [dialog, setDialog] = useState<"delete" | "edit" | null>(null), [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ item: ResourceFile; content: string; initialContent: string; version: number } | null>(null), [clipboard, setClipboard] = useState<{ mode: "copy" | "cut"; paths: string[] } | null>(null), [preview, setPreview] = useState<ResourceFile | null>(null);
  const [inlineEdit, setInlineEdit] = useState<{ path: string; value: string; creating?: boolean } | null>(null);
  const [editingAddress, setEditingAddress] = useState(false), [addressDraft, setAddressDraft] = useState("");
  const addressInput = useRef<HTMLInputElement>(null), addressDisplay = useRef<HTMLDivElement>(null), selectAddress = useRef(true);
  const [quota, setQuota] = useState<StorageAllowances | null>(null);
  const input = useRef<HTMLInputElement>(null), menuRef = useRef<HTMLDivElement>(null), anchorRef = useRef<string | null>(null), longPress = useRef<number | null>(null);
  const canWrite = role === "admin" || role === "editor", scope = session ? `${session.user.id}:${session.workspace_id}` : "";
  const current = `/resources/browse?path=${encodeURIComponent(path)}&q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&sort=${sort}&limit=500`;

  async function load(signal?: AbortSignal) {
    setLoading(true);
    try {
      const endpoint = select ? `/resources?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&limit=100` : current;
      const value = await request<{ items: (Item | ResourceFile)[]; breadcrumbs?: { name: string; path: string }[] }>(endpoint, { signal });
      if (!signal?.aborted) { setItems(select ? value.items.map(item => ({ ...item, kind: "file" as const, name: item.path.split("/").pop() || item.path } as ResourceFile & { kind: "file"; name: string })) : value.items as Item[]); setCrumbs(value.breadcrumbs ?? []); setSelected(new Set()); setError(""); }
    } catch (cause) { if (!signal?.aborted) setError(cause instanceof Error ? cause.message : "工作区加载失败，请重试"); }
    finally { if (!signal?.aborted) setLoading(false); }
  }
  useEffect(() => { const controller = new AbortController(); const timer = setTimeout(() => void load(controller.signal), query ? 180 : 0); return () => { clearTimeout(timer); controller.abort(); }; }, [session?.workspace_id, path, query, source, sort, select]);
  useEffect(() => { try { setView(localStorage.getItem(`resource-view:${session?.user.id}`) === "list" ? "list" : "grid"); } catch {} }, [session?.user.id]);
  useEffect(() => { if (select || !session) return; const controller = new AbortController(); void request<StorageAllowances>("/storage/allowances", { signal: controller.signal }).then(value => !controller.signal.aborted && setQuota(value)).catch(() => {}); return () => controller.abort(); }, [session?.workspace_id, select]);
  useEffect(() => { if (editingAddress) { addressInput.current?.focus(); if (selectAddress.current) addressInput.current?.select(); } }, [editingAddress]);

  function editAddress() {
    selectAddress.current = true;
    setAddressDraft(path ? `工作区/${path}` : "工作区");
    setEditingAddress(true);
  }
  function closeAddress() {
    setEditingAddress(false);
    requestAnimationFrame(() => addressDisplay.current?.focus());
  }
  function navigateAddress() {
    const destination = addressDraft.trim().replace(/\\/g, "/").replace(/^工作区(?:\/|$)/, "").replace(/^\/+|\/+$/g, "");
    if (destination.split("/").some(part => part === ".." || part === ".")) {
      setError("请输入工作区内的文件夹路径，不要包含 . 或 ..");
      return;
    }
    setPath(destination); setQuery(""); closeAddress();
  }

  function selectItem(item: Item, event: Pick<React.MouseEvent, "ctrlKey" | "metaKey" | "shiftKey">) {
    setMenu(null);
    setSelected(previous => {
      const next = new Set(event.ctrlKey || event.metaKey || event.shiftKey ? previous : []);
      if (event.shiftKey && anchorRef.current) {
        const paths = items.map(value => value.path), first = paths.indexOf([...previous][0]), last = paths.indexOf(item.path);
        const anchor = paths.indexOf(anchorRef.current);
        paths.slice(Math.min(anchor < 0 ? first : anchor, last), Math.max(anchor < 0 ? first : anchor, last) + 1).forEach(value => next.add(value));
      } else if (event.ctrlKey || event.metaKey) {
        if (next.has(item.path)) next.delete(item.path); else next.add(item.path);
      } else next.add(item.path);
      anchorRef.current = item.path;
      return next;
    });
  }
  async function open(item: Item) {
    if (item.kind === "directory") { setPath(item.path); setQuery(""); return; }
    if (select) { setBusy(true); try { await select(item); } catch (cause) { setError(cause instanceof Error ? cause.message : "选择失败，请重试"); } finally { setBusy(false); } return; }
    if (item.content_type.startsWith("text/") || /\.(md|txt|csv|json|py|js|ts|tsx|jsx|html|css|sh|yaml|yml)$/i.test(item.path)) {
      try { const value = await request<{ content: string; version: number }>(`/workspaces/${session.workspace_id}/files/${item.id}/text`); setEditor({ item, content: value.content, initialContent: value.content, version: value.version }); setDialog("edit"); } catch (cause) { setError(cause instanceof Error ? cause.message : "文件无法编辑"); }
    } else setPreview(item);
  }
  async function upload(files: File[]) {
    if (!files.length || busy) return; setBusy(true);
    try { for (const file of files) { const body = new FormData(); body.set("file", file); body.set("directory", path); await request("/resources", { method: "POST", body }); } await load(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "上传失败，请重试"); } finally { setBusy(false); }
  }
  async function createFolder() {
    if (busy) return; setBusy(true);
    const folderName = "新建文件夹";
    try {
      const created = await request<{ path: string }>("/resources/directories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path ? `${path}/${folderName}` : folderName }) });
      await load(); setSelected(new Set([created.path])); anchorRef.current = created.path; setInlineEdit({ path: created.path, value: folderName, creating: true });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "新建文件夹失败，请检查当前目录和名称冲突"); } finally { setBusy(false); }
  }
  async function saveInlineEdit() {
    if (!inlineEdit || !inlineEdit.value.trim()) return;
    const item = items.find(value => value.path === inlineEdit.path);
    if (!item) return;
    setBusy(true);
    try {
      const newPath = path ? `${path}/${inlineEdit.value.trim()}` : inlineEdit.value.trim();
      await request(item.kind === "directory" ? "/resources/directories" : `/resources/files/${(item as ResourceFile).id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: item.path, new_path: newPath }) });
      setInlineEdit(null); await load(); setSelected(new Set([newPath])); anchorRef.current = newPath;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "重命名失败，请检查名称、非法字符和同名冲突"); } finally { setBusy(false); }
  }
  async function cancelInlineEdit() {
    if (!inlineEdit) return;
    const creating = inlineEdit.creating;
    setInlineEdit(null);
    if (creating) {
      try { await request("/resources/directories", { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: inlineEdit.path }) }); await load(); }
      catch (cause) { setError(cause instanceof Error ? cause.message : "取消新建文件夹失败，请重试"); }
    }
  }
  function startRename(item: Item) {
    if (!canWrite) return;
    setMenu(null); setSelected(new Set([item.path])); anchorRef.current = item.path; setInlineEdit({ path: item.path, value: item.name });
  }
  async function removeSelected() {
    setBusy(true);
    try { for (const item of items.filter(value => selected.has(value.path))) await request(item.kind === "directory" ? "/resources/directories" : `/resources/${(item as ResourceFile).id}`, { method: "DELETE", headers: item.kind === "directory" ? { "Content-Type": "application/json" } : undefined, body: item.kind === "directory" ? JSON.stringify({ path: item.path }) : undefined }); setDialog(null); await load(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "删除失败，请重试"); } finally { setBusy(false); }
  }
  async function paste() {
    if (!clipboard) return; setBusy(true);
    try { await request(`/resources/operations/${clipboard.mode === "copy" ? "copy" : "move"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: clipboard.paths, destination: path }) }); if (clipboard.mode === "cut") setClipboard(null); await load(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "粘贴失败，请处理同名冲突后重试"); } finally { setBusy(false); }
  }
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (dialog || select || menu || event.defaultPrevented) return;
      if (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") { event.preventDefault(); setSelected(new Set(items.map(item => item.path))); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c") setClipboard({ mode: "copy", paths: [...selected] });
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "x") setClipboard({ mode: "cut", paths: [...selected] });
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") { event.preventDefault(); void paste(); }
      else if (event.key === "Delete" && selected.size && canWrite) setDialog("delete");
      else if (event.key === "F2" && selected.size === 1 && canWrite) { const item = items.find(value => selected.has(value.path)); if (item) startRename(item); }
      else if ((event.key === "Backspace" || (event.altKey && event.key === "ArrowUp")) && path) { event.preventDefault(); setPath(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""); }
      else if (event.key === "Enter" && selected.size === 1) { const item = items.find(value => value.path === [...selected][0]); if (item) void open(item); }
    }
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, [items, selected, path, clipboard, dialog, select, canWrite, menu]);

  useLayoutEffect(() => {
    if (!menu) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const element = menuRef.current;
    if (element) {
      const bounds = element.getBoundingClientRect();
      element.style.left = `${Math.max(12, Math.min(menu.x, window.innerWidth - bounds.width - 12))}px`;
      element.style.top = `${Math.max(12, Math.min(menu.y, window.innerHeight - bounds.height - 12))}px`;
    }
    const onMenuKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setMenu(null); requestAnimationFrame(() => opener?.focus()); return; }
      const buttons = menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
      if (!buttons?.length) return;
      const currentIndex = Array.from(buttons).indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); buttons[(currentIndex + (event.key === "ArrowDown" ? 1 : buttons.length - 1)) % buttons.length]?.focus(); }
      if (event.key === "Enter" && document.activeElement instanceof HTMLButtonElement) { event.preventDefault(); document.activeElement.click(); }
    };
    window.addEventListener("keydown", onMenuKey); requestAnimationFrame(() => menuRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus());
    return () => window.removeEventListener("keydown", onMenuKey);
  }, [menu]);

  const grid = !select && view === "grid";
  const search = <label className="resource-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索工作区" placeholder="搜索工作区" value={query} onChange={event => setQuery(event.target.value)} />{query && <button type="button" className="icon-button" aria-label="清除搜索" onClick={() => setQuery("")}><X size={15} /></button>}</label>;
  const sourceFilter = <select aria-label="来源" value={source} onChange={event => setSource(event.target.value)}><option value="">全部来源</option>{Object.entries(sources).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>;
  return <div className={`resource-library ${select ? "resource-picker-v2" : "workspace-explorer"}`} onClick={() => setMenu(null)}>
    {!select && <div className="workspace-address-bar">
      <Tooltip content="返回上级（Alt + ↑）"><button type="button" className="icon-button" aria-label="返回上级" disabled={!path || busy} onClick={() => { setPath(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""); setQuery(""); }}><ArrowUp size={18} /></button></Tooltip>
      <div className={`workspace-address ${editingAddress ? "workspace-address-editing" : ""}`}>
        {editingAddress ? <input ref={addressInput} aria-label="地址栏" value={addressDraft} spellCheck={false} autoComplete="off" onChange={event => setAddressDraft(event.target.value)} onBlur={() => setEditingAddress(false)} onKeyDown={event => {
          event.stopPropagation();
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Enter") { event.preventDefault(); navigateAddress(); }
          if (event.key === "Escape") { event.preventDefault(); closeAddress(); }
        }} /> : <div ref={addressDisplay} className="workspace-breadcrumbs" role="group" aria-label="当前路径，双击或按 Enter 编辑" tabIndex={0} onDoubleClick={event => { event.preventDefault(); editAddress(); }} onKeyDown={event => {
          if (event.target !== event.currentTarget) return;
          if (event.key === "Enter" || event.key === "F2") { event.preventDefault(); event.stopPropagation(); editAddress(); }
          else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); event.stopPropagation(); selectAddress.current = false; setAddressDraft(event.key); setEditingAddress(true); }
        }}>{(crumbs.length ? crumbs : [{ name: "工作区", path: "" }]).map((crumb, index, entries) => <span key={crumb.path}><button type="button" aria-current={crumb.path === path ? "location" : undefined} onClick={event => { if (event.detail > 1 || crumb.path === path) return; setPath(crumb.path); setQuery(""); }}>{crumb.name}</button>{index < entries.length - 1 && <ChevronRight size={14} />}</span>)}</div>}
      </div>
      {search}
    </div>}
    <div className="resource-browser-toolbar" role="group" aria-label={select ? "筛选文件" : "文件操作"}>
      {select ? <>{search}{sourceFilter}</> : <>
        <div className="workspace-file-commands">
          <button type="button" className="workspace-command" disabled={!canWrite || busy} onClick={() => void createFolder()}><Plus size={17} />新建</button>
          <input ref={input} type="file" accept={attachmentAccept} multiple hidden onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
          <button type="button" className="primary" disabled={!canWrite || busy} onClick={() => input.current?.click()}><Upload size={16} />上传</button>
          <span className="workspace-command-divider" aria-hidden="true" />
          <Tooltip content="复制（Ctrl + C）"><button type="button" className="icon-button" disabled={!selected.size || busy} aria-label="复制" onClick={() => setClipboard({ mode: "copy", paths: [...selected] })}><Copy size={17} /></button></Tooltip>
          <Tooltip content="剪切（Ctrl + X）"><button type="button" className="icon-button" disabled={!canWrite || !selected.size || busy} aria-label="剪切" onClick={() => setClipboard({ mode: "cut", paths: [...selected] })}><Scissors size={17} /></button></Tooltip>
          <Tooltip content="粘贴（Ctrl + V）"><button type="button" className="icon-button" disabled={!canWrite || !clipboard?.paths.length || busy} aria-label="粘贴" onClick={() => void paste()}><Clipboard size={17} /></button></Tooltip>
          <Tooltip content="删除（Delete）"><button type="button" className="icon-button" disabled={!canWrite || !selected.size || busy} aria-label="删除" onClick={() => setDialog("delete")}><Trash2 size={17} /></button></Tooltip>
        </div>
      </>}
      <div className="resource-toolbar-tail">
        {!select && <>{sourceFilter}<select aria-label="排序" value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="name">按名称</option><option value="type">按类型</option><option value="updated_at">按修改时间</option></select>
          <div className="resource-view-switch" role="group" aria-label="浏览方式">{(["grid", "list"] as const).map(mode => <Tooltip content={mode === "grid" ? "卡片" : "列表"} key={mode}><button type="button" aria-label={mode === "grid" ? "卡片" : "列表"} aria-pressed={view === mode} onClick={() => { setView(mode); try { localStorage.setItem(`resource-view:${session.user.id}`, mode); } catch {} }}>{mode === "grid" ? <LayoutGrid size={17} /> : <List size={18} />}</button></Tooltip>)}</div>
        </>}
        <Tooltip content="刷新"><button type="button" className="icon-button" aria-label="刷新工作区" disabled={loading || busy} onClick={() => void load()}><RefreshCw size={17} className={loading ? "spin" : undefined} /></button></Tooltip>
      </div>
    </div>
    {!select && <ResourceNotice />}
    {(select || query || source) && <div className="resource-browser-summary"><span>{select ? "选择文件" : query ? "搜索结果" : sources[source]}<small>{loading ? "正在加载" : `${items.length} 项${query ? " · 整个工作区" : ""}`}</small></span>{!select && <button type="button" className="workspace-command" onClick={() => { setQuery(""); setSource(""); }}>清除筛选</button>}</div>}
    {error && <div className="resource-browser-alert" role="alert"><span>{error}</span><button type="button" className="secondary" disabled={busy || loading} onClick={() => void load()}>重试</button></div>}
    <section className={`resource-browser-results ${grid ? "resource-masonry" : "resource-rows"}`} aria-label="工作区内容" aria-busy={loading} tabIndex={0}>
      {!grid && !select && <div className="workspace-list-heading" aria-hidden="true"><span>名称</span><span>类型</span><span>完整路径</span><span>大小</span><span>修改时间</span><span /></div>}
      {loading ? Array.from({ length: grid ? 8 : 4 }, (_, index) => <div key={index} className={`resource-skeleton ${grid ? "" : "resource-skeleton-row"}`} aria-hidden="true" />) : !items.length ? <div className="resource-browser-empty"><FolderOpen size={36} strokeWidth={1.3} /><h2>{query || source ? "没有找到内容" : "此文件夹为空"}</h2><p>{query || source ? "试试其他关键词或清除筛选" : canWrite ? "新建文件夹或上传文件开始整理" : "此文件夹还没有内容"}</p></div> : items.map(item => {
        const active = selected.has(item.path), editing = inlineEdit?.path === item.path;
        const updated = item.updated_at ? new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" }) : "—";
        return <article key={item.path} tabIndex={0} aria-label={item.name} draggable={canWrite && !editing} className={`resource-item ${grid ? "resource-card" : "resource-row"} ${active ? "resource-item-selected" : ""} ${item.kind === "directory" ? "resource-directory" : ""}`} onKeyDown={event => { if (event.key === " " && event.target === event.currentTarget) { event.preventDefault(); selectItem(item, event); } }} onClick={event => selectItem(item, event)} onDoubleClick={() => void open(item)} onContextMenu={event => { event.preventDefault(); if (!active) setSelected(new Set([item.path])); anchorRef.current = item.path; setMenu({ x: event.clientX, y: event.clientY, item }); }} onPointerDown={() => { if (window.matchMedia("(pointer: coarse)").matches) { longPress.current = window.setTimeout(() => { setSelected(new Set([item.path])); setMenu({ x: 16, y: 96, item }); }, 550); } }} onPointerUp={() => { if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; } }} onPointerCancel={() => { if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; } }} onDragStart={event => event.dataTransfer.setData("text/plain", item.path)} onDragOver={event => item.kind === "directory" && event.preventDefault()} onDrop={event => { if (item.kind === "directory") { event.preventDefault(); const sourcePath = event.dataTransfer.getData("text/plain"); if (sourcePath) void request("/resources/operations/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [sourcePath], destination: item.path }) }).then(() => load()).catch(cause => setError(cause instanceof Error ? cause.message : "移动失败，请处理冲突后重试")); } }}>
          {grid && active && <span className="resource-selection-check" aria-label="已选中"><Check size={14} /></span>}
          <div className="resource-item-open"><span className="workspace-file-icon" aria-hidden="true">{item.kind === "directory" ? <Folder size={grid ? 44 : 20} strokeWidth={1.4} /> : <ResourceThumbnail item={item} scope={scope} compact={!grid} />}</span>
            <div className="resource-item-copy"><span className="resource-item-name" title={item.path}>{editing ? <input className="resource-inline-input" aria-label={`重命名 ${item.name}`} autoFocus value={inlineEdit.value} onChange={event => setInlineEdit({ ...inlineEdit, value: event.target.value })} onClick={event => event.stopPropagation()} onKeyDown={event => { event.stopPropagation(); if (event.nativeEvent.isComposing) return; if (event.key === "Enter") { event.preventDefault(); void saveInlineEdit(); } if (event.key === "Escape") { event.preventDefault(); void cancelInlineEdit(); } }} onBlur={() => { if (inlineEdit?.creating) return; void saveInlineEdit(); }} /> : item.name}</span>
              <span className="resource-item-meta">{itemType(item)}{item.kind === "file" && ` · ${bytes(item.size_bytes)}`}</span>
              {(select || query || !grid) && <span className="resource-item-path" title={item.path}>{item.path}</span>}
            </div>
          </div>
          {!grid && !select && <><span className="resource-list-type">{itemType(item)}</span><span className="resource-list-path" title={item.path}>{item.path}</span><span className="resource-list-size">{item.kind === "directory" ? "—" : bytes(item.size_bytes)}</span><time className="resource-list-date" dateTime={item.updated_at}>{updated}</time></>}
          {!select && <div className="resource-item-bottom">{grid && <time dateTime={item.updated_at}>{updated}</time>}<div className="resource-item-actions"><Tooltip content="更多操作"><button type="button" className="icon-button" aria-label={`更多操作 ${item.name}`} onClick={event => { event.stopPropagation(); if (!active) setSelected(new Set([item.path])); setMenu({ x: event.clientX, y: event.clientY, item }); }}><MoreVertical size={16} /></button></Tooltip></div></div>}
        </article>;
      })}
    </section>
    {!select && <div className="workspace-file-status" role="status"><span>{loading ? "正在加载…" : `${items.length} 个项目`}</span>{selected.size > 0 && <span>已选中 {selected.size} 项</span>}{!canWrite && <span>只读</span>}{quota && <span className="workspace-file-capacity">已用 {bytes(quota.used_bytes)} / {bytes(quota.limit_bytes)}</span>}</div>}
    {menu && <div ref={menuRef} data-workspace-menu className="workspace-context-menu" style={{ left: Math.min(menu.x, window.innerWidth - 220), top: Math.min(menu.y, window.innerHeight - 300) }} role="menu" onClick={event => event.stopPropagation()}><button role="menuitem" onClick={() => { setMenu(null); void open(menu.item); }}><FolderOpen size={15} />打开</button>{menu.item.kind === "file" && <><button role="menuitem" onClick={() => { setMenu(null); void open(menu.item); }}><Edit3 size={15} />编辑</button><button role="menuitem" onClick={() => { const file = menu.item as ResourceFile; setMenu(null); void downloadFile(`/resources/${file.id}/download`, file.path.split("/").pop() || "文件"); }}><Download size={15} />下载</button></>}<button role="menuitem" disabled={!canWrite} onClick={() => startRename(menu.item)}><Pencil size={15} />重命名（F2）</button><button role="menuitem" disabled={!canWrite} onClick={() => { setClipboard({ mode: "copy", paths: [menu.item.path] }); setMenu(null); }}><Copy size={15} />复制</button><button role="menuitem" disabled={!canWrite} onClick={() => { setClipboard({ mode: "cut", paths: [menu.item.path] }); setMenu(null); }}><Clipboard size={15} />剪切</button><button role="menuitem" disabled={!canWrite} onClick={() => { setMenu(null); setSelected(new Set([menu.item.path])); setDialog("delete"); }}><Trash2 size={15} />删除</button><button role="menuitem" disabled={!canWrite} onClick={() => { setMenu(null); void createFolder(); }}><Plus size={15} />新建文件夹</button></div>}
    {dialog === "delete" && <Modal title="删除确认" busy={busy} onClose={() => setDialog(null)}><p>将删除选中的 {selected.size} 项。文件夹内文件也会从当前工作区、Skill 管理页和云电脑中消失。</p><div className="modal-footer"><button className="secondary" onClick={() => setDialog(null)}>取消</button><button className="danger" onClick={() => void removeSelected()}>删除</button></div></Modal>}
    {dialog === "edit" && editor && <Modal title={`编辑 · ${editor.item.path.split("/").pop() || "文件"}`} wide busy={busy} onClose={() => { if (editor.content !== editor.initialContent && !window.confirm("有未保存的修改，确定要关闭吗？")) return; setDialog(null); setEditor(null); }}><div className="workspace-editor"><p className="workspace-editor-path">{editor.item.path}</p><textarea autoFocus value={editor.content} onChange={event => setEditor({ ...editor, content: event.target.value })} /><div className="modal-footer"><button className="secondary" onClick={() => { if (editor.content !== editor.initialContent && !window.confirm("有未保存的修改，确定要关闭吗？")) return; setDialog(null); setEditor(null); }}>取消</button><button className="primary" onClick={async () => { setBusy(true); try { await request(`/workspaces/${session.workspace_id}/files`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: editor.item.path, content: editor.content, expected_version: editor.version, content_type: editor.item.content_type }) }); setDialog(null); setEditor(null); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败，请重新加载后重试"); } finally { setBusy(false); } }}>保存</button></div></div></Modal>}
    {preview && <ResourcePreview key={`${scope}:${preview.id}`} item={preview} scope={scope} onClose={() => setPreview(null)} />}
  </div>;
}
