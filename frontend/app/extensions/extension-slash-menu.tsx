"use client";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { Check, FileText, Loader2, Plug, RefreshCw } from "lucide-react";
import { useExtensionCatalog } from "./extension-catalog";
import type { ExtensionSelection } from "./extension-data";
import "./extensions.css";

type Option = { id: string; kind: "platform" | "skill" | "mcp"; name: string; description: string };
export type SlashMenuHandle = { keyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean };

export const ExtensionSlashMenu = forwardRef<SlashMenuHandle, {
  draft: string; caret: number; disabled: boolean; supportsTools: boolean;
  selection: ExtensionSelection | null; input: RefObject<HTMLTextAreaElement | null>;
  onChange: (selection: ExtensionSelection) => Promise<void>;
  onInsert: (draft: string, caret: number) => void;
  onActive: (open: boolean, option: string | undefined) => void;
}>(function ExtensionSlashMenu({ draft, caret, disabled, supportsTools, selection, input, onChange, onInsert, onActive }, ref) {
  const [dismissed, setDismissed] = useState(false);
  const [index, setIndex] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const menu = useRef<HTMLDivElement>(null);
  const match = draft.slice(0, caret).match(/(?:^|\s)\/([^\s/]*)$/u);
  const query = match?.[1].toLocaleLowerCase() ?? "";
  const open = !!match && !disabled && !dismissed;
  const catalog = useExtensionCatalog(open);
  const options: Option[] = [
    ...catalog.skills.filter(item => item.enabled).map(item => ({ ...item, kind: "skill" as const })),
    ...catalog.platform.map(item => ({ ...item, kind: "platform" as const })),
    ...catalog.servers.filter(item => item.enabled && item.last_checked_at && item.allowed_tools.length)
      .map(item => ({ id: item.id, kind: "mcp" as const, name: item.name, description: `${item.allowed_tools.length} 项已授权工具` })),
  ].filter(item => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query)).slice(0, 30);
  const activeIndex = Math.min(index, Math.max(0, options.length - 1));
  const effective = selection ?? { skill_ids: catalog.skills.filter(item => item.enabled).map(item => item.id),
    mcp_server_ids: catalog.servers.filter(item => item.enabled && item.last_checked_at && item.allowed_tools.length).map(item => item.id) };
  const ready = open && !catalog.loading && !catalog.error && supportsTools && !saving;
  useEffect(() => { setDismissed(false); setIndex(0); setError(""); }, [draft, caret]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onActive(open, ready && options.length ? `extension-slash-${activeIndex}` : undefined); }, [open, ready, activeIndex, options.length, onActive]);
  useEffect(() => {
    if (!open) return;
    function close(event: PointerEvent) {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && target !== input.current) setDismissed(true);
    }
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open, input]);
  async function choose(item: Option) {
    if (!ready) return;
    setSaving(true); setError("");
    try {
      if (item.kind === "skill" && !effective.skill_ids.includes(item.id))
        await onChange({ ...effective, skill_ids: [...effective.skill_ids, item.id] });
      else if (item.kind === "mcp" && !effective.mcp_server_ids.includes(item.id))
        await onChange({ ...effective, mcp_server_ids: [...effective.mcp_server_ids, item.id] });
      if (!alive.current) return;
      const start = caret - query.length - 1;
      const inserted = `使用「${item.name}」 `;
      onInsert(draft.slice(0, start) + inserted + draft.slice(caret), start + inserted.length);
      setDismissed(true);
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "选择未保存，请重试"); }
    finally { if (alive.current) setSaving(false); }
  }
  useImperativeHandle(ref, () => ({ keyDown(event) {
    if (!open || event.nativeEvent.isComposing) return false;
    if (event.key === "Escape") { event.preventDefault(); setDismissed(true); return true; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setIndex(options.length ? (activeIndex + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length : 0);
      return true;
    }
    if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey) {
      if (event.key === "Tab" && (!ready || !options.length)) return false;
      event.preventDefault(); if (ready && options[activeIndex]) void choose(options[activeIndex]); return true;
    }
    return false;
  } }));
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>(`#extension-slash-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);
  if (!open) return null;
  return <div className="extension-slash-menu" ref={menu}>
    <div className="extension-slash-heading"><span>选择扩展</span><kbd>/</kbd>{saving && <Loader2 size={15} className="spin" aria-label="正在选择" />}</div>
    {!supportsTools ? <p role="status">当前模型无法使用扩展，请先更换模型。</p>
      : catalog.loading ? <p role="status">正在加载扩展…</p>
      : catalog.error ? <div role="alert"><p>{catalog.error}</p><button className="text-button" type="button" onClick={catalog.retry}><RefreshCw size={14} />重试</button></div>
      : <div id="composer-extension-options" role="listbox" aria-label="选择扩展" className="extension-slash-options">
        {options.map((item, i) => <button key={`${item.kind}:${item.id}`} id={`extension-slash-${i}`} type="button"
          role="option" aria-selected={i === activeIndex} tabIndex={-1} disabled={saving}
          onMouseDown={event => event.preventDefault()} onMouseEnter={() => setIndex(i)} onClick={() => void choose(item)}>
          {item.kind === "mcp" ? <Plug size={17} /> : <FileText size={17} />}
          <span className="extension-slash-copy"><strong>{item.name}</strong><small>{item.description}</small></span>
          <span className="extension-slash-source">{item.kind === "platform" ? "平台 Skill" : item.kind === "skill" ? "我的 Skill" : "MCP"}</span>
          {(item.kind === "skill" && effective.skill_ids.includes(item.id) || item.kind === "mcp" && effective.mcp_server_ids.includes(item.id)) && <Check size={15} aria-label="已选择" />}
        </button>)}
        {!options.length && <p role="status">{query ? "没有匹配的扩展" : "暂无可用扩展"}</p>}
      </div>}
    {error && <p role="alert">{error}</p>}
  </div>;
});
