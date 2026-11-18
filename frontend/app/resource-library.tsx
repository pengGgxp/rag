"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, Check, ChevronRight, Clipboard, Copy, Download, Edit3, Folder, FolderOpen, LayoutGrid, List, MoreVertical, Pencil, Plus, RefreshCw, Scissors, Search, Trash2, Upload, X } from "lucide-react";
import { ApiError, downloadFile, request } from "./api";
import { useAuth } from "./auth";
import { Modal } from "./modal";
import { useAdminChanges } from "./admin/admin-changes";
import { bytes, type StorageAllowances } from "./storage-types";
import { Tooltip } from "../components/ui/tooltip";
import { ResourceNotice } from "./resource-notice";
import { ResourcePreview, ResourceThumbnail } from "./resource-preview";
import { useResourceFilePreview } from "./resource-file-link";
import { workspaceFileId } from "./workspace-file-link";
import "./resource-library.css";

export type ResourceFile = { id: string; path: string; content_type: string; size_bytes: number; source: string; updated_at: string; revision?: string; availability?: "pending" | "missing" | "available" };
type Item = (ResourceFile & { kind: "file"; name: string }) | { kind: "directory"; name: string; path: string; revision?: string; updated_at?: string; size_bytes?: number; content_type?: string };
type DeletePreview = { paths: string[]; files: number; directories: number; confirmation: string };
type FileConflict = { path: string; kind: string; revision?: string; replaceable?: boolean };
type ConflictChoice = "keep_both" | "replace";
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
  const [dialog, setDialog] = useState<"delete" | "edit" | null>(null), [busy, setBusyState] = useState(false);
  const busyRef = useRef(false);
  function setBusy(value: boolean) { busyRef.current = value; setBusyState(value); }
  const [editor, setEditor] = useState<{ item: ResourceFile; content: string; initialContent: string; revision: string } | null>(null), [clipboard, setClipboard] = useState<{ mode: "copy" | "cut"; paths: string[]; expected: Record<string, string> } | null>(null), [preview, setPreview] = useState<ResourceFile | null>(null);
  const [inlineEdit, setInlineEdit] = useState<{ path: string; value: string; creating?: "directory" | "file" } | null>(null);
  const pendingItem = useRef<Item | null>(null);
  const inlineInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!inlineEdit || loading) return;
    const frame = requestAnimationFrame(() => { inlineInput.current?.focus(); inlineInput.current?.select(); });
    return () => cancelAnimationFrame(frame);
  }, [inlineEdit?.path, loading]);
  const [hasMore, setHasMore] = useState(false), [total, setTotal] = useState(0), [loadingMore, setLoadingMore] = useState(false);
  const [newMenu, setNewMenu] = useState<HTMLElement | null>(null), [deletePreview, setDeletePreview] = useState<DeletePreview | null>(null);
  const [conflict, setConflict] = useState<{ items: FileConflict[]; choices?: Record<string, ConflictChoice>; retry: (choice: ConflictChoice, resolutions?: Record<string, ConflictChoice>) => Promise<void> } | null>(null);
  const [discard, setDiscard] = useState<false | "close" | "reload">(false), [editorError, setEditorError] = useState("");
  const [editingAddress, setEditingAddress] = useState(false), [addressDraft, setAddressDraft] = useState("");
  const addressInput = useRef<HTMLInputElement>(null), addressDisplay = useRef<HTMLDivElement>(null), selectAddress = useRef(true);
  const [quota, setQuota] = useState<StorageAllowances | null>(null);
  const input = useRef<HTMLInputElement>(null), menuRef = useRef<HTMLDivElement>(null), anchorRef = useRef<string | null>(null), longPress = useRef<number | null>(null);
  const loadGeneration = useRef(0);
  const touchPress = useRef<{ x: number; y: number; opened: boolean } | null>(null);
  function cancelLongPress() {
    if (longPress.current) window.clearTimeout(longPress.current);
    longPress.current = null;
  }
  function itemPointerDown(item: Item, event: React.PointerEvent) {
    if (event.pointerType !== "touch") return;
    cancelLongPress();
    touchPress.current = { x: event.clientX, y: event.clientY, opened: false };
    longPress.current = window.setTimeout(() => {
      if (!touchPress.current) return;
      touchPress.current.opened = true;
      setSelected(previous => previous.has(item.path) ? previous : new Set([item.path]));
      anchorRef.current = item.path;
      setMenu({ x: 16, y: 96, item });
    }, 550);
  }
  function itemPointerMove(event: React.PointerEvent) {
    const start = touchPress.current;
    if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) cancelLongPress();
  }
  function itemClick(item: Item, event: React.MouseEvent) {
    if (touchPress.current?.opened) {
      touchPress.current = null;
      event.preventDefault(); event.stopPropagation();
      return;
    }
    touchPress.current = null;
    selectItem(item, event);
  }
  useEffect(() => () => cancelLongPress(), []);
  const canWrite = role === "admin" || role === "editor", scope = session ? `${session.user.id}:${session.workspace_id}` : "";
  const linkedFile = useResourceFilePreview(scope);
  const openLinkedFile = linkedFile.open;
  useEffect(() => {
    if (select || !session) return;
    const id = workspaceFileId(window.location.href, window.location.origin);
    if (id) void openLinkedFile(id);
  }, [select, session?.workspace_id, openLinkedFile]);
  const current = `/resources/browse?path=${encodeURIComponent(path)}&q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&sort=${sort}&limit=100`;
  useAdminChanges(Boolean(editor && editor.content !== editor.initialContent), busy);
  useEffect(() => { if (!select) setPath(new URLSearchParams(window.location.search).get("path") || ""); }, [select]);

  async function load(signal?: AbortSignal, append = false) {
    const generation = ++loadGeneration.current;
    if (append) setLoadingMore(true); else setLoading(true);
    try {
      const endpoint = (select ? `/resources?q=${encodeURIComponent(query)}&source=${encodeURIComponent(source)}&limit=100` : current) + `&offset=${append ? items.length : 0}`;
      const value = await request<{ items: (Item | ResourceFile)[]; has_more: boolean; total?: number; breadcrumbs?: { name: string; path: string }[] }>(endpoint, { signal });
      if (!signal?.aborted && generation === loadGeneration.current) { const rows = select ? value.items.map(item => ({ ...item, kind: "file" as const, name: item.path.split("/").pop() || item.path } as ResourceFile & { kind: "file"; name: string })) : value.items as Item[]; setItems(previous => append ? [...previous, ...rows] : pendingItem.current ? [pendingItem.current, ...rows] : rows); setHasMore(value.has_more); setTotal(value.total ?? (append ? items.length : 0) + rows.length); setCrumbs(value.breadcrumbs ?? []); if (!append) setSelected(new Set(pendingItem.current ? [pendingItem.current.path] : [])); setError(""); }
    } catch (cause) { if (!signal?.aborted && generation === loadGeneration.current) setError(cause instanceof Error ? cause.message : "工作区加载失败，请重试"); }
    finally { if (!signal?.aborted && generation === loadGeneration.current) { setLoading(false); setLoadingMore(false); } }
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
    if (busyRef.current || loading) return;
    if (item.kind === "directory") { setPath(item.path); setQuery(""); return; }
    if (select) { setBusy(true); try { await select(item); } catch (cause) { setError(cause instanceof Error ? cause.message : "选择失败，请重试"); } finally { setBusy(false); } return; }
    setPreview(item);
  }
  async function edit(item: ResourceFile) {
    if (item.content_type.startsWith("text/") || /\.(md|txt|csv|json|py|js|ts|tsx|jsx|html|css|sh|yaml|yml)$/i.test(item.path)) {
      try { const value = await request<{ content: string; revision: string }>(`/workspaces/${session.workspace_id}/files/${item.id}/text`); setEditor({ item, content: value.content, initialContent: value.content, revision: value.revision }); setEditorError(""); setDiscard(false); setDialog("edit"); } catch (cause) { setError(cause instanceof Error ? cause.message : "文件无法打开"); }
    } else setPreview(item);
  }
  function showConflict(cause: unknown, retry: (choice: ConflictChoice, expected: Record<string, string>, resolutions?: Record<string, ConflictChoice>) => Promise<void>) {
    const detail = cause instanceof ApiError ? cause.detail as { conflicts?: FileConflict[] } | undefined : undefined;
    if (!detail?.conflicts?.length) return false;
    const conflicts = detail.conflicts;
    setConflict({ items: conflicts, retry: (choice, resolutions) => retry(choice, Object.fromEntries(conflicts.filter(item => item.revision).map(item => [item.path, item.revision!])), resolutions) });
    return true;
  }
  async function upload(files: File[], choice = "error", expected: Record<string, string> = {}) {
    if (!files.length || busyRef.current || !canWrite) return; setBusy(true); setError("");
    for (let index = 0; index < files.length; index++) {
      const file = files[index], target = path ? `${path}/${file.name}` : file.name;
      try { const body = new FormData(); body.set("file", file); body.set("directory", path); body.set("conflict", choice); body.set("request_id", crypto.randomUUID()); if (expected[target]) body.set("revision", expected[target]); await request("/resources", { method: "POST", body }); }
      catch (cause) {
        showConflict(cause, (next, versions) => upload(files.slice(index), next, versions)) || setError(cause instanceof Error ? cause.message : "上传失败，请重新选择文件重试");
        setBusy(false); await load(); return;
      }
    }
    setBusy(false); await load();
  }
  function createNew(kind: "directory" | "file") {
    if (busy || !canWrite) return;
    setNewMenu(null); setQuery("");
    const base = kind === "directory" ? "新建文件夹" : "新建文本文档.txt";
    let name = base, count = 2;
    while (items.some(item => item.name === name)) { name = kind === "file" ? `新建文本文档 (${count++}).txt` : `${base} (${count++})`; }
    const pendingPath = path ? `${path}/${name}` : name;
    const pending: Item = kind === "directory" ? { kind, path: pendingPath, name } : { kind, path: pendingPath, name, id: "pending", content_type: "text/plain", size_bytes: 0, source: "resource_upload", updated_at: new Date().toISOString() };
    pendingItem.current = pending;
    setItems(previous => [pending, ...previous]); setSelected(new Set([pendingPath]));
    setInlineEdit({ path: pendingPath, value: name, creating: kind });
  }
  async function saveInlineEdit() {
    if (!inlineEdit || busy) return;
    if (!inlineEdit.value.trim()) { setError("名称不能为空，请输入文件或文件夹名称"); return; }
    const item = items.find(value => value.path === inlineEdit.path);
    if (!item) return;
    const parent = item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "";
    const newPath = parent ? `${parent}/${inlineEdit.value.trim()}` : inlineEdit.value.trim();
    setBusy(true); setError("");
    try {
      if (inlineEdit.creating === "directory") await request("/resources/directories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: newPath, request_id: crypto.randomUUID() }) });
      else if (inlineEdit.creating === "file") await request(`/workspaces/${session.workspace_id}/files`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: newPath, content: "", content_type: "text/plain", request_id: crypto.randomUUID() }) });
      else await request(item.kind === "directory" ? "/resources/directories" : `/resources/files/${(item as ResourceFile).id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: item.path, new_path: newPath, revision: item.revision, request_id: crypto.randomUUID() }) });
      pendingItem.current = null; setInlineEdit(null); await load(); setSelected(new Set([newPath])); anchorRef.current = newPath;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "名称无效或已存在，请修改名称后重试"); }
    finally { setBusy(false); }
  }
  function cancelInlineEdit() {
    if (inlineEdit?.creating) setItems(previous => previous.filter(item => item.path !== inlineEdit.path));
    pendingItem.current = null;
    setInlineEdit(null);
  }
  function startRename(item: Item) {
    if (!canWrite || busy) return;
    setMenu(null); setSelected(new Set([item.path])); anchorRef.current = item.path; setInlineEdit({ path: item.path, value: item.name });
  }
  function copySelection(mode: "copy" | "cut") {
    setClipboard({ mode, paths: [...selected], expected: Object.fromEntries(items.filter(item => selected.has(item.path) && item.revision).map(item => [item.path, item.revision!])) }); setMenu(null);
  }
  async function beginDelete() {
    if (!canWrite || busy) return;
    setMenu(null); setBusy(true); setError("");
    try { const value = await request<DeletePreview>("/resources/operations/delete-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: [...selected] }) }); setDeletePreview(value); setDialog("delete"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "无法确认删除范围，请刷新后重新选择"); }
    finally { setBusy(false); }
  }
  async function removeSelected() {
    if (!deletePreview) return;
    setBusy(true);
    try { await request("/resources/operations/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths: deletePreview.paths, confirmation: deletePreview.confirmation, request_id: crypto.randomUUID() }) }); setDialog(null); setDeletePreview(null); await load(); }
    catch (cause) { const detail = cause instanceof ApiError ? cause.detail as { preview?: DeletePreview } | undefined : undefined; if (detail?.preview) setDeletePreview(detail.preview); setError(cause instanceof Error ? cause.message : "删除失败，请重试"); }
    finally { setBusy(false); }
  }
  async function transfer(paths: string[], destination: string, mode: "copy" | "cut", expected: Record<string, string> = {}, choice = "error", resolutions: Record<string, ConflictChoice> = {}) {
    if (!canWrite || busyRef.current) return; setBusy(true); setError("");
    try { await request(`/resources/operations/${mode === "copy" ? "copy" : "move"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths, destination, expected, conflict: choice, resolutions, request_id: crypto.randomUUID() }) }); if (mode === "cut") setClipboard(null); await load(); }
    catch (cause) { showConflict(cause, (next, versions, choices) => transfer(paths, destination, mode, { ...expected, ...versions }, next, { ...resolutions, ...choices })) || setError(cause instanceof Error ? cause.message : "操作失败，请刷新后重试"); }
    finally { setBusy(false); }
  }
  async function paste() { if (clipboard) await transfer(clipboard.paths, path, clipboard.mode, clipboard.expected); }
  async function selectAll() {
    if (busyRef.current || loading) return;
    setBusy(true);
    try {
      let rows = items, more = hasMore;
      while (more) {
        const page = await request<{ items: Item[]; has_more: boolean }>(`${current}&offset=${rows.length}`);
        rows = [...rows, ...page.items]; more = page.has_more;
      }
      setItems(rows); setHasMore(false); setSelected(new Set(rows.map(item => item.path)));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法选择全部项目，请重试"); }
    finally { setBusy(false); }
  }
  function closeEditor() { if (editor && editor.content !== editor.initialContent) { setDiscard("close"); return; } setDialog(null); setEditor(null); }
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (dialog || select || menu || busyRef.current || event.defaultPrevented) return;
      if (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") { event.preventDefault(); void selectAll(); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c") { event.preventDefault(); copySelection("copy"); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "x" && canWrite) { event.preventDefault(); copySelection("cut"); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") { event.preventDefault(); void paste(); }
      else if (event.key === "Delete" && selected.size && canWrite) { event.preventDefault(); void beginDelete(); }
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
          <button type="button" className="workspace-command" disabled={!canWrite || busy} aria-expanded={!!newMenu} onClick={event => setNewMenu(event.currentTarget)}><Plus size={17} />新建</button>
          <input ref={input} type="file" multiple hidden onChange={event => { void upload(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
          <button type="button" className="primary" disabled={!canWrite || busy} onClick={() => input.current?.click()}><Upload size={16} />上传</button>
          <span className="workspace-command-divider" aria-hidden="true" />
          <Tooltip content="复制（Ctrl + C）"><button type="button" className="icon-button" disabled={!selected.size || busy} aria-label="复制" onClick={() => copySelection("copy")}><Copy size={17} /></button></Tooltip>
          <Tooltip content="剪切（Ctrl + X）"><button type="button" className="icon-button" disabled={!canWrite || !selected.size || busy} aria-label="剪切" onClick={() => copySelection("cut")}><Scissors size={17} /></button></Tooltip>
          <Tooltip content="粘贴（Ctrl + V）"><button type="button" className="icon-button" disabled={!canWrite || !clipboard?.paths.length || busy} aria-label="粘贴" onClick={() => void paste()}><Clipboard size={17} /></button></Tooltip>
          <Tooltip content="删除（Delete）"><button type="button" className="icon-button" disabled={!canWrite || !selected.size || busy} aria-label="删除" onClick={() => void beginDelete()}><Trash2 size={17} /></button></Tooltip>
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
    {(select || query || source) && <div className="resource-browser-summary"><span>{select ? "选择文件" : query ? "搜索结果" : sources[source]}<small>{loading ? "正在加载" : `${total} 项${query ? " · 整个工作区" : ""}`}</small></span>{!select && <button type="button" className="workspace-command" onClick={() => { setQuery(""); setSource(""); }}>清除筛选</button>}</div>}
    {error && <div className="resource-browser-alert" role="alert"><span>{error}</span><button type="button" className="secondary" disabled={busy || loading} onClick={() => void load()}>重试</button></div>}
    <section className={`resource-browser-results ${grid ? "resource-masonry" : "resource-rows"}`} aria-label="工作区内容" role="listbox" aria-multiselectable="true" aria-busy={loading} tabIndex={0}>
      {!grid && !select && <div className="workspace-list-heading" aria-hidden="true"><span>名称</span><span>类型</span><span>完整路径</span><span>大小</span><span>修改时间</span><span /></div>}
      {loading ? Array.from({ length: grid ? 8 : 4 }, (_, index) => <div key={index} className={`resource-skeleton ${grid ? "" : "resource-skeleton-row"}`} aria-hidden="true" />) : !items.length ? <div className="resource-browser-empty"><FolderOpen size={36} strokeWidth={1.3} /><h2>{query || source ? "没有找到内容" : "此文件夹为空"}</h2><p>{query || source ? "试试其他关键词或清除筛选" : canWrite ? "新建文件夹或上传文件开始整理" : "此文件夹还没有内容"}</p></div> : items.map(item => {
        const active = selected.has(item.path), editing = inlineEdit?.path === item.path;
        const updated = item.updated_at ? new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "short", day: "numeric" }) : "—";
        return <article key={item.path} role="option" aria-selected={active} tabIndex={0} aria-label={item.name} draggable={canWrite && !editing} className={`resource-item ${grid ? "resource-card" : "resource-row"} ${active ? "resource-item-selected" : ""} ${item.kind === "directory" ? "resource-directory" : ""}`} onKeyDown={event => { if (event.key === " " && event.target === event.currentTarget) { event.preventDefault(); selectItem(item, event); } }} onClick={event => itemClick(item, event)} onDoubleClick={() => void open(item)} onContextMenu={event => { event.preventDefault(); if (!active) setSelected(new Set([item.path])); anchorRef.current = item.path; setMenu({ x: event.clientX, y: event.clientY, item }); }} onPointerDown={event => itemPointerDown(item, event)} onPointerMove={itemPointerMove} onPointerUp={cancelLongPress} onPointerCancel={cancelLongPress} onDragStart={event => { event.dataTransfer.setData("application/x-rag-paths", JSON.stringify(active ? [...selected] : [item.path])); event.dataTransfer.effectAllowed = "move"; }} onDragOver={event => item.kind === "directory" && event.preventDefault()} onDrop={event => { if (item.kind === "directory") { event.preventDefault(); try { const paths: string[] = JSON.parse(event.dataTransfer.getData("application/x-rag-paths") || "[]"); if (paths.length) void transfer(paths, item.path, "cut", Object.fromEntries(items.filter(value => paths.includes(value.path) && value.revision).map(value => [value.path, value.revision!]))); } catch { setError("请选择工作区中的文件后拖动"); } } }}>
          {grid && active && <span className="resource-selection-check" aria-label="已选中"><Check size={14} /></span>}
          <div className="resource-item-open"><span className="workspace-file-icon" aria-hidden="true">{item.kind === "directory" ? <Folder size={grid ? 44 : 20} strokeWidth={1.4} /> : <ResourceThumbnail item={item} scope={scope} compact={!grid} />}</span>
            <div className="resource-item-copy"><span className="resource-item-name" title={item.path}>{editing ? <input ref={inlineInput} className="resource-inline-input" aria-label={inlineEdit.creating ? "输入新项目名称" : `重命名 ${item.name}`} autoFocus value={inlineEdit.value} onChange={event => setInlineEdit({ ...inlineEdit, value: event.target.value })} onClick={event => event.stopPropagation()} onKeyDown={event => { event.stopPropagation(); if (event.nativeEvent.isComposing) return; if (event.key === "Enter") { event.preventDefault(); void saveInlineEdit(); } if (event.key === "Escape") { event.preventDefault(); void cancelInlineEdit(); } }} /> : item.name}</span>
              <span className="resource-item-meta">{itemType(item)}{item.kind === "file" && ` · ${bytes(item.size_bytes)}`}</span>
              {(select || query || !grid) && <span className="resource-item-path" title={item.path}>{item.path}</span>}
            </div>
          </div>
          {!grid && !select && <><span className="resource-list-type">{itemType(item)}</span><span className="resource-list-path" title={item.path}>{item.path}</span><span className="resource-list-size">{item.kind === "directory" ? "—" : bytes(item.size_bytes)}</span><time className="resource-list-date" dateTime={item.updated_at}>{updated}</time></>}
          {!select && <div className="resource-item-bottom">{grid && <time dateTime={item.updated_at}>{updated}</time>}<div className="resource-item-actions"><Tooltip content="更多操作"><button type="button" className="icon-button" aria-label={`更多操作 ${item.name}`} onClick={event => { event.stopPropagation(); if (!active) setSelected(new Set([item.path])); setMenu({ x: event.clientX, y: event.clientY, item }); }}><MoreVertical size={16} /></button></Tooltip></div></div>}
        </article>;
      })}
    </section>
    {!select && <div className="workspace-file-status" role="status"><span>{loading ? "正在加载…" : `${total} 个项目${hasMore ? ` · 已加载 ${items.length}` : ""}`}</span>{selected.size > 0 && <span>已选中 {selected.size} 项</span>}{!canWrite && <span>只读</span>}{quota && <span className="workspace-file-capacity">已用 {bytes(quota.used_bytes)} / {bytes(quota.limit_bytes)}</span>}</div>}
    {hasMore && <div className="workspace-load-more"><button className="secondary" disabled={loadingMore || busy} onClick={() => void load(undefined, true)}>{loadingMore ? "正在加载…" : `继续加载（已显示 ${items.length} 项）`}</button></div>}
    {newMenu && <Modal title="新建" anchor={newMenu} restoreFocusSelector=".resource-inline-input" onClose={() => setNewMenu(null)}><div className="workspace-new-options"><button className="workspace-command" onClick={() => createNew("directory")}><Folder size={17} />文件夹</button><button className="workspace-command" onClick={() => createNew("file")}><Edit3 size={17} />文本文档</button></div></Modal>}
    {menu && <div ref={menuRef} data-workspace-menu className="workspace-context-menu" style={{ left: menu.x, top: menu.y }} role="menu" onClick={event => event.stopPropagation()}>
      <button role="menuitem" onClick={() => { setMenu(null); void open(menu.item); }}><FolderOpen size={15} />打开</button>
      {menu.item.kind === "file" && <><button role="menuitem" disabled={!canWrite} onClick={() => { setMenu(null); void edit(menu.item as ResourceFile); }}><Edit3 size={15} />编辑</button><button role="menuitem" onClick={() => { const file = menu.item as ResourceFile; setMenu(null); void downloadFile(`/resources/${file.id}/download`, file.path.split("/").pop() || "文件").catch(cause => setError(String(cause))); }}><Download size={15} />下载</button></>}
      <button role="menuitem" disabled={!canWrite || selected.size !== 1} onClick={() => startRename(menu.item)}><Pencil size={15} />重命名（F2）</button>
      <button role="menuitem" onClick={() => copySelection("copy")}><Copy size={15} />复制{selected.size > 1 ? ` ${selected.size} 项` : ""}</button>
      <button role="menuitem" disabled={!canWrite} onClick={() => copySelection("cut")}><Clipboard size={15} />剪切</button>
      <button role="menuitem" disabled={!canWrite} onClick={() => void beginDelete()}><Trash2 size={15} />删除{selected.size > 1 ? ` ${selected.size} 项` : ""}</button>
      <button role="menuitem" disabled={!canWrite || !!query} onClick={() => { setMenu(null); createNew("directory"); }}><Plus size={15} />新建文件夹</button>
    </div>}
    {conflict && <Modal title="目标位置有同名项目" busy={busy} onClose={() => setConflict(null)}><ul className="workspace-conflict-list">{conflict.items.map(item => <li key={item.path}><span>{item.path}{item.kind === "type_mismatch" && "（文件与文件夹不能互相替换）"}</span>{conflict.items.length > 1 && <select aria-label={`处理 ${item.path}`} value={conflict.choices?.[item.path] || ""} onChange={event => setConflict({ ...conflict, choices: { ...conflict.choices, [item.path]: event.target.value as ConflictChoice } })}><option value="" disabled>选择处理方式</option><option value="keep_both">保留两份</option><option value="replace" disabled={item.replaceable === false}>{item.kind === "directory" ? "合并文件夹" : "替换"}</option></select>}</li>)}</ul><div className="modal-footer"><button autoFocus className="secondary" onClick={() => setConflict(null)}>取消</button><button className="secondary" disabled={busy} onClick={() => { const pending = conflict; setConflict(null); void pending.retry("keep_both"); }}>保留两份</button><button className="danger" disabled={busy || conflict.items.some(item => item.replaceable === false)} onClick={() => { const pending = conflict; setConflict(null); void pending.retry("replace"); }}>{conflict.items.every(item => item.kind === "directory") ? "合并文件夹" : "替换"}</button>{conflict.items.length > 1 && <button className="primary" disabled={busy || conflict.items.some(item => !conflict.choices?.[item.path])} onClick={() => { const pending = conflict; setConflict(null); void pending.retry("replace", pending.choices); }}>应用所选操作</button>}</div></Modal>}
    {dialog === "delete" && deletePreview && <Modal title="删除确认" busy={busy} onClose={() => setDialog(null)}><p>将删除 {deletePreview.files} 个文件和 {deletePreview.directories} 个文件夹。同一工作区的所有成员及云电脑将无法再访问这些内容。</p>{error && <p role="alert" className="extension-error">{error}</p>}<div className="modal-footer"><button autoFocus className="secondary" onClick={() => setDialog(null)}>取消</button><button className="danger" disabled={busy} onClick={() => void removeSelected()}>确认删除</button></div></Modal>}
    {dialog === "edit" && editor && <Modal title={`${canWrite ? "编辑" : "预览"} · ${editor.item.path.split("/").pop() || "文件"}`} wide busy={busy} onClose={closeEditor}><div className="workspace-editor"><p className="workspace-editor-path">{editor.item.path}</p><textarea aria-label="文件内容" autoFocus readOnly={!canWrite} disabled={busy} value={editor.content} onChange={event => setEditor({ ...editor, content: event.target.value })} />
      {editorError && <div role="alert" className="resource-browser-alert"><span>{editorError}</span><button className="secondary" onClick={() => setDiscard("reload")}>重新加载</button></div>}
      {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的修改？</span><button className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button className="danger" onClick={() => { const reload = discard === "reload"; setDiscard(false); setDialog(null); setEditor(null); if (reload) void open({ ...editor.item, kind: "file", name: editor.item.path.split("/").pop() || "文件" }); }}>放弃修改</button></div>}
      <div className="modal-footer"><button className="secondary" onClick={closeEditor}>{canWrite ? "取消" : "关闭"}</button>{canWrite && <button className="primary" disabled={busy} onClick={async () => { setBusy(true); setEditorError(""); try { await request(`/workspaces/${session.workspace_id}/files`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: editor.item.path, content: editor.content, revision: editor.revision, request_id: crypto.randomUUID(), content_type: editor.item.content_type }) }); setDialog(null); setEditor(null); await load(); } catch (cause) { setEditorError(cause instanceof Error ? cause.message : "保存失败，内容已保留，请重试"); } finally { setBusy(false); } }}>保存</button>}</div></div></Modal>}
    {linkedFile.error && <p role="alert" className="resource-error">{linkedFile.error}</p>}
    {linkedFile.preview}
    {preview && <ResourcePreview key={`${scope}:${preview.id}`} item={preview} scope={scope} onClose={() => setPreview(null)}
      restoreFocusSelector={`.resource-item[aria-label="${CSS.escape(preview.path)}"]`} />}
  </div>;
}
