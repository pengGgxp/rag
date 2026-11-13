"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Clipboard, Copy, Download, Edit3, Folder, FolderOpen, LayoutGrid, List, MoreVertical, Pencil, Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";
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

export function ResourceLibrary({ select }: { select?: (item: ResourceFile) => Promise<void> }) {
  const { session, role } = useAuth();
  const [items, setItems] = useState<Item[]>([]), [path, setPath] = useState(""), [query, setQuery] = useState(""), [source, setSource] = useState(""), [sort, setSort] = useState<"name" | "type" | "updated_at">("name");
  const [crumbs, setCrumbs] = useState<{ name: string; path: string }[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [view, setView] = useState<"grid" | "list">("grid"), [selected, setSelected] = useState<Set<string>>(new Set()), [menu, setMenu] = useState<{ x: number; y: number; item: Item } | null>(null);
  const [dialog, setDialog] = useState<"delete" | "edit" | null>(null), [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ item: ResourceFile; content: string; initialContent: string; version: number } | null>(null), [clipboard, setClipboard] = useState<{ mode: "copy" | "cut"; paths: string[] } | null>(null), [preview, setPreview] = useState<ResourceFile | null>(null);
  const [inlineEdit, setInlineEdit] = useState<{ path: string; value: string; creating?: boolean } | null>(null);
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

  function selectItem(item: Item, event: React.MouseEvent) {
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
      if (dialog || select) return;
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
  }, [items, selected, path, clipboard, dialog, select, canWrite]);

  useEffect(() => {
    if (!menu) return;
    const onMenuKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setMenu(null); return; }
      const buttons = menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
      if (!buttons?.length) return;
      const currentIndex = Array.from(buttons).indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); buttons[(currentIndex + (event.key === "ArrowDown" ? 1 : buttons.length - 1)) % buttons.length]?.focus(); }
      if (event.key === "Enter" && document.activeElement instanceof HTMLButtonElement) document.activeElement.click();
    };
    window.addEventListener("keydown", onMenuKey); requestAnimationFrame(() => menuRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus());
    return () => window.removeEventListener("keydown", onMenuKey);
  }, [menu]);

  const grid = !select && view === "grid";
  return <div className={`resource-library ${select ? "resource-picker-v2" : ""}`} onClick={() => setMenu(null)}>
    <div className="resource-browser-toolbar">
      <label className="resource-search"><Search size={17} /><input aria-label="搜索工作区" placeholder="搜索工作区…" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <select aria-label="来源" value={source} onChange={event => setSource(event.target.value)}><option value="">全部来源</option>{Object.entries(sources).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
      {!select && <select aria-label="排序" value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="name">按名称</option><option value="type">按类型</option><option value="updated_at">按修改时间</option></select>}
      <div className="resource-toolbar-tail">
        {!select && <><button type="button" className="secondary" disabled={!canWrite || busy} onClick={() => void createFolder()}><Plus size={16} />新建</button><input ref={input} type="file" accept={attachmentAccept} multiple hidden onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ""; }} /><button type="button" className="primary" disabled={!canWrite || busy} onClick={() => input.current?.click()}><Upload size={16} />上传</button>{clipboard && <button type="button" className="secondary" disabled={busy} onClick={() => void paste()}><Clipboard size={16} />粘贴</button>}<Tooltip content="复制"><button type="button" className="icon-button" disabled={!selected.size} aria-label="复制" onClick={() => setClipboard({ mode: "copy", paths: [...selected] })}><Copy size={17} /></button></Tooltip><Tooltip content="剪切"><button type="button" className="icon-button" disabled={!canWrite || !selected.size} aria-label="剪切" onClick={() => setClipboard({ mode: "cut", paths: [...selected] })}><Clipboard size={17} /></button></Tooltip><Tooltip content="删除"><button type="button" className="icon-button" disabled={!canWrite || !selected.size} aria-label="删除" onClick={() => setDialog("delete")}><Trash2 size={17} /></button></Tooltip></>}
        {!select && <div className="resource-view-switch" role="group" aria-label="浏览方式">{(["grid", "list"] as const).map(mode => <Tooltip content={mode === "grid" ? "卡片" : "列表"} key={mode}><button type="button" aria-label={mode === "grid" ? "卡片" : "列表"} aria-pressed={view === mode} onClick={() => { setView(mode); try { localStorage.setItem(`resource-view:${session.user.id}`, mode); } catch {} }}>{mode === "grid" ? <LayoutGrid size={17} /> : <List size={18} />}</button></Tooltip>)}</div>}
        <Tooltip content="刷新"><button type="button" className="icon-button" aria-label="刷新工作区" disabled={loading || busy} onClick={() => void load()}><RefreshCw size={17} /></button></Tooltip>
      </div>
    </div>
    {!select && <><div className="workspace-address-bar"><button type="button" className="icon-button" aria-label="返回上级" disabled={!path} onClick={() => setPath(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "")}><ChevronLeft size={17} /></button><div className="workspace-breadcrumbs">{crumbs.map((crumb, index) => <span key={crumb.path}><button type="button" onClick={() => setPath(crumb.path)}>{crumb.name}</button>{index < crumbs.length - 1 && <ChevronRight size={14} />}</span>)}</div><input aria-label="地址栏" value={path || "工作区"} onFocus={event => event.currentTarget.select()} onChange={event => setPath(event.target.value === "工作区" ? "" : event.target.value.replace(/^工作区\/?/, ""))} onKeyDown={event => { if (event.key === "Enter") void load(); }} /></div><ResourceNotice /></>}
    <div className="resource-browser-summary"><span>{select ? "选择文件" : path ? `工作区 · ${path}` : "工作区"}<small>{items.length}</small></span>{!select && quota && <span>已用 {bytes(quota.used_bytes)}<span className="resource-summary-divider">/</span>{bytes(quota.limit_bytes)}</span>}</div>
    {error && <p className="resource-browser-alert" role="alert">{error}</p>}
    <section className={`resource-browser-results ${grid ? "resource-masonry" : "resource-rows"}`} aria-label="工作区内容" tabIndex={0}>
      {loading ? Array.from({ length: grid ? 8 : 4 }, (_, index) => <div key={index} className={`resource-skeleton ${grid ? "" : "resource-skeleton-row"}`} aria-hidden="true" />) : !items.length ? <div className="resource-browser-empty"><FolderOpen size={40} strokeWidth={1.15} /><h2>{query ? "没有找到内容" : "此文件夹为空"}</h2><p>{query ? "试试其他关键词" : "新建文件夹或上传文件开始整理工作区"}</p></div> : items.map(item => { const active = selected.has(item.path); const editing = inlineEdit?.path === item.path; return <article key={item.path} draggable={canWrite && !editing} className={`resource-item ${grid ? "resource-card" : "resource-row"} ${active ? "resource-item-selected" : ""}`} onClick={event => selectItem(item, event)} onDoubleClick={() => void open(item)} onContextMenu={event => { event.preventDefault(); if (!active) setSelected(new Set([item.path])); anchorRef.current = item.path; setMenu({ x: event.clientX, y: event.clientY, item }); }} onPointerDown={() => { if (window.matchMedia("(pointer: coarse)").matches) { longPress.current = window.setTimeout(() => { setSelected(new Set([item.path])); setMenu({ x: 16, y: 96, item }); }, 550); } }} onPointerUp={() => { if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; } }} onPointerCancel={() => { if (longPress.current) { clearTimeout(longPress.current); longPress.current = null; } }} onDragStart={event => event.dataTransfer.setData("text/plain", item.path)} onDragOver={event => item.kind === "directory" && event.preventDefault()} onDrop={event => { if (item.kind === "directory") { event.preventDefault(); const sourcePath = event.dataTransfer.getData("text/plain"); if (sourcePath) void request("/resources/operations/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [sourcePath], destination: item.path }) }).then(() => load()).catch(cause => setError(cause instanceof Error ? cause.message : "移动失败，请处理冲突后重试")); } }}><div className="resource-item-open"><span className="workspace-file-icon">{item.kind === "directory" ? <Folder size={20} /> : <ResourceThumbnail item={item} scope={scope} compact={!grid} />}</span><div className="resource-item-copy"><span className="resource-item-name" title={item.path}>{editing ? <input className="resource-inline-input" aria-label={`重命名 ${item.name}`} autoFocus value={inlineEdit.value} onChange={event => setInlineEdit({ ...inlineEdit, value: event.target.value })} onClick={event => event.stopPropagation()} onKeyDown={event => { event.stopPropagation(); if (event.key === "Enter") { event.preventDefault(); void saveInlineEdit(); } if (event.key === "Escape") { event.preventDefault(); void cancelInlineEdit(); } }} onBlur={() => { if (inlineEdit?.creating) return; void saveInlineEdit(); }} /> : item.name}</span><span className="resource-item-meta">{item.kind === "directory" ? "文件夹" : `${bytes(item.size_bytes)} · ${sources[item.source] ?? "文件"}`}</span>{!grid && <span className="resource-item-path">{item.path}</span>}</div></div>{!select && <div className="resource-item-bottom"><time>{item.updated_at ? new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" }) : ""}</time><div className="resource-item-actions"><Tooltip content="更多操作"><button type="button" className="icon-button" aria-label={`更多操作 ${item.name}`} onClick={event => { event.stopPropagation(); setMenu({ x: event.clientX, y: event.clientY, item }); }}><MoreVertical size={16} /></button></Tooltip></div></div>}</article>; })}
    </section>
    {menu && <div ref={menuRef} data-workspace-menu className="workspace-context-menu" style={{ left: Math.min(menu.x, window.innerWidth - 220), top: Math.min(menu.y, window.innerHeight - 300) }} role="menu" onClick={event => event.stopPropagation()}><button role="menuitem" onClick={() => { setMenu(null); void open(menu.item); }}><FolderOpen size={15} />打开</button>{menu.item.kind === "file" && <><button role="menuitem" onClick={() => { setMenu(null); void open(menu.item); }}><Edit3 size={15} />编辑</button><button role="menuitem" onClick={() => { const file = menu.item as ResourceFile; setMenu(null); void downloadFile(`/resources/${file.id}/download`, file.path.split("/").pop() || "文件"); }}><Download size={15} />下载</button></>}<button role="menuitem" disabled={!canWrite} onClick={() => startRename(menu.item)}><Pencil size={15} />重命名（F2）</button><button role="menuitem" disabled={!canWrite} onClick={() => { setClipboard({ mode: "copy", paths: [menu.item.path] }); setMenu(null); }}><Copy size={15} />复制</button><button role="menuitem" disabled={!canWrite} onClick={() => { setClipboard({ mode: "cut", paths: [menu.item.path] }); setMenu(null); }}><Clipboard size={15} />剪切</button><button role="menuitem" disabled={!canWrite} onClick={() => { setMenu(null); setSelected(new Set([menu.item.path])); setDialog("delete"); }}><Trash2 size={15} />删除</button><button role="menuitem" disabled={!canWrite} onClick={() => { setMenu(null); void createFolder(); }}><Plus size={15} />新建文件夹</button></div>}
    {dialog === "delete" && <Modal title="删除确认" busy={busy} onClose={() => setDialog(null)}><p>将删除选中的 {selected.size} 项。文件夹内文件也会从当前工作区、Skill 管理页和云电脑中消失。</p><div className="modal-footer"><button className="secondary" onClick={() => setDialog(null)}>取消</button><button className="danger" onClick={() => void removeSelected()}>删除</button></div></Modal>}
    {dialog === "edit" && editor && <Modal title={`编辑 · ${editor.item.path.split("/").pop() || "文件"}`} wide busy={busy} onClose={() => { if (editor.content !== editor.initialContent && !window.confirm("有未保存的修改，确定要关闭吗？")) return; setDialog(null); setEditor(null); }}><div className="workspace-editor"><p className="workspace-editor-path">{editor.item.path}</p><textarea autoFocus value={editor.content} onChange={event => setEditor({ ...editor, content: event.target.value })} /><div className="modal-footer"><button className="secondary" onClick={() => { if (editor.content !== editor.initialContent && !window.confirm("有未保存的修改，确定要关闭吗？")) return; setDialog(null); setEditor(null); }}>取消</button><button className="primary" onClick={async () => { setBusy(true); try { await request(`/workspaces/${session.workspace_id}/files`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: editor.item.path, content: editor.content, expected_version: editor.version, content_type: editor.item.content_type }) }); setDialog(null); setEditor(null); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败，请重新加载后重试"); } finally { setBusy(false); } }}>保存</button></div></div></Modal>}
    {preview && <ResourcePreview key={`${scope}:${preview.id}`} item={preview} scope={scope} onClose={() => setPreview(null)} />}
  </div>;
}
