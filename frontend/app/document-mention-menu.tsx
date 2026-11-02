"use client";

import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { FileText, Loader2, RefreshCw } from "lucide-react";
import { request } from "./api";

export type MentionDocument = { id: string; filename: string; knowledge_base_id: string };
export type MentionMenuHandle = { keyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean };
type DocumentItem = MentionDocument & { status: string; is_active: boolean };

export const DocumentMentionMenu = forwardRef<MentionMenuHandle, {
  draft: string;
  caret: number;
  disabled: boolean;
  supportsTools: boolean;
  libraries: { id: string; name: string }[];
  selected: MentionDocument[];
  input: RefObject<HTMLTextAreaElement | null>;
  onChoose: (item: MentionDocument, start: number, end: number) => void;
  onActive: (open: boolean, option: string | undefined) => void;
}>(function DocumentMentionMenu({ draft, caret, disabled, supportsTools, libraries, selected, input, onChoose, onActive }, ref) {
  const [dismissed, setDismissed] = useState(false);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const menu = useRef<HTMLDivElement>(null);
  const candidate = draft.slice(0, caret).match(/(?:^|\s)@([^@\n]*)$/u);
  const match = candidate && !selected.some(item => candidate[1] === item.filename
    || candidate[1].startsWith(`${item.filename} `)) ? candidate : null;
  const query = match?.[1] ?? "";
  const start = match ? caret - query.length - 1 : -1;
  const open = !!match && !disabled && !dismissed;

  useEffect(() => { setDismissed(false); setIndex(0); setError(""); }, [draft, caret]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setError("");
    Promise.all(libraries.map(item => request<DocumentItem[]>(`/knowledge-bases/${encodeURIComponent(item.id)}/documents`)))
      .then(groups => { if (active) setDocuments(groups.flat()); })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : "文档加载失败"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open, libraries, attempt]);

  const selectedIds = new Set(selected.map(item => item.id));
  const options = documents.filter(item => item.status === "ready" && item.is_active && !selectedIds.has(item.id)
    && item.filename.toLocaleLowerCase().includes(query.toLocaleLowerCase())).slice(0, 30);
  const activeIndex = Math.min(index, Math.max(0, options.length - 1));
  const ready = open && !loading && !error && supportsTools;

  useEffect(() => {
    onActive(open, ready && options.length ? `document-mention-${activeIndex}` : undefined);
  }, [open, ready, activeIndex, options.length, onActive]);
  useEffect(() => {
    if (!open) return;
    function close(event: PointerEvent) {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && target !== input.current) setDismissed(true);
    }
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open, input]);
  useEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>(`#document-mention-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

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
      event.preventDefault();
      const item = options[activeIndex];
      if (item && ready && selected.length < 8) onChoose({ id: item.id, filename: item.filename, knowledge_base_id: item.knowledge_base_id }, start, caret);
      return true;
    }
    return false;
  } }));

  if (!open) return null;
  return <div className="extension-slash-menu document-mention-menu" ref={menu}>
    <div className="extension-slash-heading"><span>文档</span><kbd>@</kbd></div>
    {selected.length >= 8 ? <p role="status">每次最多选择 8 篇文档</p>
      : !supportsTools ? <p role="status">当前模型无法检索文档，请选择支持工具的模型。</p>
      : loading ? <p role="status"><Loader2 size={14} className="spin" />正在查找文档…</p>
      : error ? <div role="alert"><p>{error}</p><button type="button" className="text-button" onClick={() => setAttempt(value => value + 1)}><RefreshCw size={14} />重试</button></div>
      : <div id="composer-document-options" role="listbox" aria-label="提及文档" className="extension-slash-options">
        {options.map((item, i) => <button key={item.id} id={`document-mention-${i}`} type="button" role="option"
          aria-selected={i === activeIndex} tabIndex={-1}
          onMouseDown={event => event.preventDefault()} onMouseEnter={() => setIndex(i)}
          onClick={() => onChoose({ id: item.id, filename: item.filename, knowledge_base_id: item.knowledge_base_id }, start, caret)}>
          <FileText size={17} />
          <span className="extension-slash-copy"><strong>{item.filename}</strong><small>{libraries.find(library => library.id === item.knowledge_base_id)?.name ?? "知识库"}</small></span>
          <span className="extension-slash-source">文档</span>
        </button>)}
        {!options.length && <p role="status">{query ? "没有匹配的可用文档" : libraries.length ? "这些知识库暂无可用文档" : "请先选择知识库"}</p>}
      </div>}
  </div>;
});
