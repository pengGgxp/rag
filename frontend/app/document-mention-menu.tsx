"use client";

import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { BookOpen, FileText, Loader2, RefreshCw } from "lucide-react";
import { request } from "./api";

export type MentionDocument = {
  id: string;
  filename: string;
  knowledge_base_id: string;
  knowledge_base_name?: string;
};
export type MentionKnowledgeBase = { id: string; name: string };
export type MentionReference =
  | (MentionDocument & { kind: "document"; knowledge_base_name?: string })
  | (MentionKnowledgeBase & { kind: "knowledge_base" });
export type MentionMenuHandle = { keyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean };
type Catalog = {
  knowledge_bases: MentionKnowledgeBase[];
  documents: (MentionDocument & { knowledge_base_name: string })[];
};

function referenceName(item: MentionReference) {
  return item.kind === "knowledge_base" ? item.name : item.filename;
}

function referenceTokens(item: MentionReference) {
  if (item.kind === "knowledge_base") return [`@知识库/${item.name}`];
  return [...new Set([
    `@${item.filename}（${item.knowledge_base_name ?? "知识库"}）`,
    `@${item.filename}`,
  ])];
}

export const DocumentMentionMenu = forwardRef<MentionMenuHandle, {
  draft: string;
  caret: number;
  disabled: boolean;
  supportsTools: boolean;
  selected: MentionReference[];
  input: RefObject<HTMLTextAreaElement | null>;
  onChoose: (item: MentionReference, start: number, end: number) => void;
  onActive: (open: boolean, option: string | undefined) => void;
}>(function DocumentMentionMenu({ draft, caret, disabled, supportsTools, selected, input, onChoose, onActive }, ref) {
  const [dismissed, setDismissed] = useState(false);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [catalog, setCatalog] = useState<Catalog>({ knowledge_bases: [], documents: [] });
  const menu = useRef<HTMLDivElement>(null);
  const candidate = draft.slice(0, caret).match(/(?:^|\s)@([^@\n]*)$/u);
  const query = candidate?.[1] ?? "";
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const selectedToken = selected.some(item => referenceTokens(item).some(token =>
    query === token.slice(1) || query.startsWith(`${token.slice(1)} `)));
  const match = candidate && !selectedToken ? candidate : null;
  const start = match ? caret - query.length - 1 : -1;
  const open = !!match && !disabled && !dismissed;

  useEffect(() => { setDismissed(false); setIndex(0); setError(""); }, [draft, caret]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const timer = window.setTimeout(() => {
      request<Catalog>(`/conversations/knowledge-catalog?q=${encodeURIComponent(query.trim())}`,
        { signal: controller.signal })
        .then(value => {
          if (!controller.signal.aborted) setCatalog(value);
        })
        .catch(reason => {
          if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "资料加载失败");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, normalizedQuery ? 140 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [open, query, normalizedQuery, attempt]);

  const selectedIds = new Set(selected.map(item => `${item.kind}:${item.id}`));
  const knowledgeBases: MentionReference[] = catalog.knowledge_bases
    .filter(item => item.name.toLocaleLowerCase().includes(normalizedQuery)
      && !selectedIds.has(`knowledge_base:${item.id}`))
    .map(item => ({ ...item, kind: "knowledge_base" as const }));
  const documents: MentionReference[] = catalog.documents
    .filter(item => (`${item.filename} ${item.knowledge_base_name}`).toLocaleLowerCase().includes(normalizedQuery)
      && !selectedIds.has(`document:${item.id}`))
    .map(item => ({ ...item, kind: "document" as const }));
  const options = [...knowledgeBases, ...documents].slice(0, 40);
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
      if (item && ready && selected.length < 8) onChoose(item, start, caret);
      return true;
    }
    return false;
  } }));

  if (!open) return null;
  const renderOption = (item: MentionReference, optionIndex: number) => {
    const isLibrary = item.kind === "knowledge_base";
    const name = referenceName(item);
    const libraryName = isLibrary ? "" : item.knowledge_base_name
      ?? catalog.knowledge_bases.find(library => library.id === item.knowledge_base_id)?.name
      ?? "知识库";
    return <button key={`${item.kind}:${item.id}`} id={`document-mention-${optionIndex}`} type="button" role="option"
      aria-selected={optionIndex === activeIndex} tabIndex={-1}
      onMouseDown={event => event.preventDefault()} onMouseEnter={() => setIndex(optionIndex)}
      onClick={() => onChoose(item, start, caret)}>
      {isLibrary ? <BookOpen size={17} /> : <FileText size={17} />}
      <span className="extension-slash-copy"><strong>{name}</strong>
        {!isLibrary && <small>{libraryName}</small>}</span>
      <span className="extension-slash-source">{isLibrary ? "知识库" : "文档"}</span>
    </button>;
  };
  return <div className="extension-slash-menu document-mention-menu" ref={menu}>
    <div className="extension-slash-heading"><span>知识库与文档</span><kbd>@</kbd></div>
    {selected.length >= 8 ? <p role="status">每条消息最多提及 8 项资料</p>
      : !supportsTools ? <p role="status">当前模型无法检索资料，请先更换模型。</p>
      : loading ? <p role="status"><Loader2 size={14} className="spin" />正在查找资料…</p>
      : error ? <div role="alert"><p>{error}</p><button type="button" className="text-button" onClick={() => setAttempt(value => value + 1)}><RefreshCw size={14} />重试</button></div>
      : <div id="composer-document-options" role="listbox" aria-label="提及知识库或文档" className="extension-slash-options">
        {!!knowledgeBases.length && <>
          <div className="document-mention-group">知识库</div>
          {knowledgeBases.map(item => renderOption(item, options.indexOf(item)))}
        </>}
        {!!documents.length && <>
          <div className="document-mention-group">文档</div>
          {documents.map(item => renderOption(item, options.indexOf(item)))}
        </>}
        {!options.length && <p role="status">{normalizedQuery ? "没有匹配的知识库或文档" : "暂无可用知识库或文档"}</p>}
      </div>}
  </div>;
});
