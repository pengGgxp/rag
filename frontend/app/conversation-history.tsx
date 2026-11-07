"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Loader2, MoreHorizontal, Pencil, Pin, Search, Trash2, X } from "lucide-react";
import { request } from "./api";
import { Modal } from "./modal";
import { Tooltip } from "../components/ui/tooltip";
import "./conversation-management.css";

export type HistoryItem = {
  id: string; title: string; revision: number; pinned_at?: string | null;
  updated_at: string; active_request_id?: string | null;
};
type SearchItem = { id: string; title: string; updated_at: string; snippet: string; message_id: string | null };
type Page<T> = { items: T[]; next_cursor: string | null };

function group(item: HistoryItem) {
  if (item.pinned_at) return "置顶";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const date = new Date(item.updated_at);
  return date >= today ? "今天" : date >= yesterday ? "昨天" : "更早";
}

export function ConversationHistory({ items, selected, onSelect, onRename, onDelete, onPin, libraries, cursor, onMore, busy, error, retry }: {
  items: HistoryItem[]; selected?: string; onSelect: (id: string, message?: string) => void;
  onRename: (item: HistoryItem) => void; onDelete: (item: HistoryItem) => void;
  onPin: (item: HistoryItem) => void; libraries: { id: string; name: string }[];
  cursor: string | null; onMore: () => void; busy: boolean; error: string; retry: () => void;
}) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [menu, setMenu] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  useEffect(() => {
    const close = () => setMenu(null);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => { window.removeEventListener("resize", close); window.removeEventListener("scroll", close, true); };
  }, []);
  return <>
    <button className="history-search-trigger" onClick={() => setSearchOpen(true)}><Search size={16} /><span>搜索对话</span></button>
    <nav className="conversation-list managed-history" aria-label="历史对话">
      {["置顶", "今天", "昨天", "更早"].map(label => {
        const rows = items.filter(item => group(item) === label);
        return rows.length ? <section key={label} aria-label={label}><h2 className="history-heading">{label}</h2>
          {rows.map(item => <div key={item.id} className={`conversation-item ${selected === item.id ? "selected" : ""}`}>
            <button title={item.title} aria-current={selected === item.id ? "page" : undefined} onClick={() => onSelect(item.id)}>
              <span>{item.title}</span>{item.active_request_id && <span className="history-running" aria-label="正在回答" />}
            </button>
            <details className="history-more" open={menu === item.id} onToggle={event => {
              if (event.currentTarget.open) setMenu(item.id); else setMenu(value => value === item.id ? null : value);
            }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMenu(null); }} onKeyDown={event => {
              if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setMenu(null); event.currentTarget.querySelector("summary")?.focus(); }
            }}>
              <summary aria-label={`管理对话 ${item.title}`} onClick={event => {
                const rect = event.currentTarget.getBoundingClientRect();
                setMenuPosition({ top: Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - 158)),
                  left: Math.max(8, Math.min(rect.right - 164, window.innerWidth - 172)) });
              }}><MoreHorizontal size={17} /></summary>
              <div role="group" aria-label="对话操作" style={{ position: "fixed", ...menuPosition }}>
                <button onClick={() => { setMenu(null); onRename(item); }}><Pencil size={15} />重命名</button>
                <button onClick={() => { setMenu(null); onPin(item); }}><Pin size={15} />{item.pinned_at ? "取消置顶" : "置顶"}</button>
                <button className="history-delete" onClick={() => { setMenu(null); onDelete(item); }}><Trash2 size={15} />删除</button>
              </div>
            </details>
          </div>)}</section> : null;
      })}
      {busy && <p className="sidebar-empty" role="status">正在加载…</p>}
      {error && <div className="history-error" role="alert">{error}<button onClick={retry}>重试</button></div>}
      {!busy && !error && !items.length && <p className="sidebar-empty">从一段新对话开始</p>}
      {cursor && <button className="history-load-more" disabled={busy} onClick={onMore}>加载更多</button>}
    </nav>
    {searchOpen && <ConversationSearch libraries={libraries} onClose={() => setSearchOpen(false)} onSelect={(id, message) => {
      setSearchOpen(false); onSelect(id, message);
    }} />}
  </>;
}

function ConversationSearch({ libraries, onSelect, onClose }: {
  libraries: { id: string; name: string }[]; onSelect: (id: string, message?: string) => void; onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [period, setPeriod] = useState("");
  const [library, setLibrary] = useState("");
  const [attachments, setAttachments] = useState(false);
  const [page, setPage] = useState<Page<SearchItem>>({ items: [], next_cursor: null });
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [active, setActive] = useState(0);
  const requestEpoch = useRef(0);
  const results = useRef<HTMLDivElement>(null);
  useEffect(() => { setCursor(null); setActive(0); setPage({ items: [], next_cursor: null }); }, [query, period, library, attachments]);
  useEffect(() => {
    const controller = new AbortController();
    const epoch = ++requestEpoch.current;
    if (!query.trim()) { setBusy(false); setError(""); return; }
    setBusy(true); setError("");
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ q: query.trim() });
      if (cursor) params.set("cursor", cursor);
      if (period) params.set("since", new Date(Date.now() - Number(period) * 86400000).toISOString());
      if (library) params.set("knowledge_base_id", library);
      if (attachments) params.set("has_attachments", "true");
      request<Page<SearchItem>>(`/conversations/search?${params}`, { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted && epoch === requestEpoch.current)
          setPage(previous => ({ ...value, items: cursor ? [...previous.items, ...value.items] : value.items }));
      }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "搜索失败，请重试"); })
        .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, period, library, attachments, cursor, attempt]);
  useEffect(() => { results.current?.querySelectorAll<HTMLElement>("[data-search-result]")[active]?.scrollIntoView({ block: "nearest" }); }, [active]);
  return <Modal title="搜索对话" onClose={onClose} wide><div className="conversation-search-panel" onKeyDown={event => {
    if (event.nativeEvent.isComposing || !page.items.length || event.target instanceof HTMLSelectElement) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); setActive(value => Math.max(0, Math.min(page.items.length - 1, value + (event.key === "ArrowDown" ? 1 : -1))));
    }
    if (event.key === "Enter" && event.target instanceof HTMLInputElement && event.target.type === "search") {
      event.preventDefault(); const item = page.items[active]; onSelect(item.id, item.message_id ?? undefined);
    }
  }}>
    <label className="conversation-search-input"><Search size={20} /><input autoFocus type="search" maxLength={200}
      placeholder="查找标题或聊天内容" aria-label="查找标题或聊天内容" value={query} onChange={event => setQuery(event.target.value)}
      aria-controls="conversation-search-results" aria-activedescendant={page.items[active] ? `search-${page.items[active].id}` : undefined} />
      {busy && <Loader2 size={18} className="spin" />}</label>
    <div className="conversation-search-filters">
      <select aria-label="时间范围" value={period} onChange={event => setPeriod(event.target.value)}><option value="">全部时间</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="90">最近 90 天</option></select>
      <select aria-label="知识库筛选" value={library} onChange={event => setLibrary(event.target.value)}><option value="">全部知识库</option>{libraries.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <label><input type="checkbox" checked={attachments} onChange={event => setAttachments(event.target.checked)} />包含附件</label>
    </div>
    <div className="conversation-search-results" id="conversation-search-results" ref={results}>
      {error && <div role="alert" className="search-state">{error}<button className="secondary" onClick={() => setAttempt(value => value + 1)}>重试</button></div>}
      {!query.trim() && <p className="search-state">找回之前聊过的内容</p>}
      {!!query.trim() && !busy && !error && !page.items.length && <p className="search-state">没有找到相关对话</p>}
      {page.items.map((item, index) => <button data-search-result id={`search-${item.id}`} className={active === index ? "active" : ""} key={item.id} title={item.title}
        onFocus={() => setActive(index)} onClick={() => onSelect(item.id, item.message_id ?? undefined)}>
        <span className="search-result-heading"><strong>{item.title}</strong><time>{new Date(item.updated_at).toLocaleDateString()}</time></span>
        <span className="search-result-snippet">{item.snippet}</span>
      </button>)}
      {page.next_cursor && <button disabled={busy} className="history-load-more" onClick={() => setCursor(page.next_cursor)}>更多结果</button>}
    </div>
  </div></Modal>;
}

export function InlineConversationTitle({ title, busy, error, value, editing, onStart, onChange, onSave, onCancel }: {
  title: string; busy: boolean; error: string; value: string; editing: boolean;
  onStart: () => void; onChange: (value: string) => void; onSave: () => void; onCancel: () => void;
}) {
  return editing ? <div className="inline-conversation-title"><form onSubmit={event => { event.preventDefault(); onSave(); }}>
    <input aria-label="对话标题" autoFocus maxLength={200} required value={value} disabled={busy}
      onChange={event => onChange(event.target.value)} onKeyDown={event => {
        if (event.nativeEvent.isComposing) { if (event.key === "Enter") event.preventDefault(); return; }
        if (event.key === "Escape") { event.preventDefault(); onCancel(); }
      }} />
    <Tooltip content="保存标题"><button className="icon-button" aria-label="保存标题" disabled={busy || !value.trim()}><Check size={17} /></button></Tooltip>
    <Tooltip content="取消编辑"><button type="button" className="icon-button" aria-label="取消编辑" disabled={busy} onClick={onCancel}><X size={17} /></button></Tooltip>
  </form>{error && <span role="alert" className="form-error">{error}</span>}</div>
    : <Tooltip content="重命名对话"><button className="conversation-title-button" onClick={onStart}><h1>{title}</h1></button></Tooltip>;
}
