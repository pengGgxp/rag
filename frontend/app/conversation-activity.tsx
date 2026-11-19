"use client";

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Check, ChevronRight, CircleAlert, CircleDashed, CornerDownLeft, ExternalLink, FileText, Loader2, Search, Square, Wrench } from "lucide-react";
import { Motion } from "../components/ui/motion";
import { ApiError, request } from "./api";
import Link from "next/link";
import { latestImageTask } from "./image-task-state";
import { ImageTaskActivity, type ImageTaskResult } from "./image-task-result";
import { PreviewResultAction, type Preview } from "./cloud-preview";
import { processHeadline } from "./conversation-display";
import type { WorkspaceFileReference } from "./workspace-file-link";
import { useMessageFiles } from "./conversation-files";

export type SavedDocument = {
  save_id: string; document_id: string; knowledge_base_id: string; filename: string;
  document_status: string; terminal: boolean; summary: string;
};

export type KnowledgeBaseResult = { id: string; name: string };

export type ConversationActivityItem = {
  id: string;
  label: string;
  summary?: string;
  executionId?: string;
  kind: "retrieval" | "tool" | "compaction";
  status: "waiting" | "running" | "completed" | "feedback" | "blocked" | "failed" | "cancelled" | "uncertain";
  files?: WorkspaceFileReference[];
  deliverables?: WorkspaceFileReference[];
  saved_document?: SavedDocument;
  image_task?: ImageTaskResult;
  preview?: Preview;
  knowledge_base?: KnowledgeBaseResult;
};

function SavedDocumentResult({ initial, onUpdate, showStatus }: { initial: SavedDocument; onUpdate: (value: SavedDocument) => void; showStatus: boolean }) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState("");
  const element = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false, visible = false, timer: ReturnType<typeof setTimeout> | undefined;
    let terminal = false, busy = false;
    const refresh = async () => {
      if (disposed || busy || terminal || !visible || document.hidden) return;
      busy = true;
      try {
        const next = await request<SavedDocument>(`/conversation-document-saves/${initial.save_id}`);
        if (disposed) return;
        setValue(next); onUpdate(next); setError(""); terminal = next.terminal;
      } catch (cause) {
        if (disposed) return;
        terminal = cause instanceof ApiError && [401, 403, 404].includes(cause.status);
        setError(terminal ? "资料暂不可访问" : "进度更新失败，可打开资料查看");
      } finally {
        busy = false;
        if (!disposed && !terminal) timer = setTimeout(refresh, 5000);
      }
    };
    const resume = () => { if (timer) clearTimeout(timer); void refresh(); };
    const observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; if (visible) resume(); });
    if (element.current) observer.observe(element.current);
    document.addEventListener("visibilitychange", resume);
    return () => { disposed = true; if (timer) clearTimeout(timer); observer.disconnect(); document.removeEventListener("visibilitychange", resume); };
  }, [initial.save_id, onUpdate]);
  return <div ref={element} className="activity-file-downloads">
    <Link className="activity-file-download" href={`/?library=${encodeURIComponent(value.knowledge_base_id)}&view=documents&document=${encodeURIComponent(value.document_id)}`}>
      <FileText size={15} aria-hidden="true" /><span>{value.filename}</span>
    </Link>
    {(error || showStatus) && <span className="activity-step-summary" role="status">{error || value.summary}</span>}
  </div>;
}

const statusLabel = {
  waiting: "等待",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  feedback: "已反馈模型",
  blocked: "操作受限",
  cancelled: "已取消",
  uncertain: "结果待确认",
};

function StatusIcon({ status }: { status: ConversationActivityItem["status"] }) {
  if (status === "running") return <Loader2 size={14} className="spin" />;
  if (status === "completed") return <Check size={14} />;
  if (status === "failed" || status === "blocked" || status === "uncertain") return <CircleAlert size={14} />;
  if (status === "feedback") return <CornerDownLeft size={14} />;
  if (status === "cancelled") return <Square size={12} />;
  return <CircleDashed size={14} />;
}

// Only presentation state lives here. Tool execution and safe summaries belong to the server.
export function ConversationActivity({ items, finished, workspaceId, detailRoot, turnFinished, steps, thinking, children }: {
  items: ConversationActivityItem[];
  finished: boolean;
  workspaceId?: string;
  detailRoot?: string;
  turnFinished?: boolean;
  steps?: ({ type: "activity"; id: string } | { type: "detail"; id: string; content: ReactNode })[];
  thinking?: boolean;
  children?: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { if (turnFinished) setExpanded(false); }, [turnFinished]);
  const regionId = useId();
  const messageFiles = useMessageFiles();
  const publishImage = messageFiles?.updateImage;
  const [savedResults, setSavedResults] = useState<Record<string, SavedDocument>>({});
  const [imageResults, setImageResults] = useState<Record<string, ImageTaskResult>>({});
  const updateImage = useCallback((value: ImageTaskResult) => {
    setImageResults(old => ({
      ...old, [value.task_id]: old[value.task_id] ? latestImageTask(old[value.task_id], value) : value,
    }));
    publishImage?.(value);
  }, [publishImage]);
  const updateSaved = useCallback((value: SavedDocument) => setSavedResults(old => ({ ...old, [value.save_id]: value })), []);
  items = items.map(item => {
    const image = item.image_task && latestImageTask(item.image_task, imageResults[item.image_task.task_id]);
    if (image) return { ...item, image_task: image, summary: image.summary, status: (!image.terminal ? "running" : image.status === "uncertain" ? "uncertain" : ["failed", "save_failed"].includes(image.status) ? "failed" : image.status === "cancelled" ? "cancelled" : "completed") as ConversationActivityItem["status"] };
    const saved = item.saved_document && (savedResults[item.saved_document.save_id] || item.saved_document);
    return saved ? { ...item, summary: saved.summary,
      status: (saved.document_status === "failed" || saved.document_status === "deleted" ? "failed"
        : saved.terminal ? "completed" : "running") as ConversationActivityItem["status"] } : item;
  });
  if (!items.length && !steps?.length) return null;
  const current = items.find(item => item.status === "running");
  const headline = processHeadline(items, finished, thinking);
  const notices = [...new Map(items.filter(item => ["blocked", "uncertain"].includes(item.status)
    || item.status === "failed" && (item.image_task || item.saved_document))
    .map(item => [item.image_task?.task_id ?? item.saved_document?.save_id ?? item.id, item])).values()];
  const ordered = steps ?? items.map(item => ({ type: "activity" as const, id: item.id }));
  const itemById = new Map(items.map(item => [item.id, item]));
  const savedDocuments = [...new Map(items.flatMap(item => item.saved_document
    ? [[item.saved_document.save_id, item.saved_document] as const] : [])).values()];
  const imageTasks = [...new Map(items.flatMap(item => item.image_task
    ? [[item.image_task.task_id, item.image_task] as const] : [])).values()];
  const previews = [...new Map(items.flatMap(item => item.preview
    ? [[item.preview.id, item.preview] as const] : [])).values()];
  return <div className="conversation-activity">
    <button type="button" className="activity-toggle" aria-expanded={expanded}
      aria-controls={regionId} aria-label={`${expanded ? "收起" : "展开"}处理过程`}
      onClick={() => setExpanded(!expanded)}>
      <ChevronRight size={14} className={expanded ? "activity-chevron expanded" : "activity-chevron"} aria-hidden="true" />
      {current || !finished ? <Loader2 size={14} className="spin activity-current-icon" aria-hidden="true" />
        : <Wrench size={14} aria-hidden="true" />}
      <span role="status">{headline}</span>
    </button>
    {notices.map(item => <div className={`activity-notice activity-${item.status}`} role="status" key={item.id}>
      <CircleAlert size={14} aria-hidden="true" />
      <span>{item.label} · {statusLabel[item.status]}{item.summary ? `：${item.summary}` : ""}</span>
    </div>)}
    {savedDocuments.map(saved => <SavedDocumentResult key={saved.save_id} initial={saved} onUpdate={updateSaved} showStatus />)}
    {imageTasks.map(image => <ImageTaskActivity key={image.task_id} initial={image} onUpdate={updateImage}
      showStatus={false} showFiles={false} />)}
    {items.filter(item => item.knowledge_base && item.status === "completed").map(item => <div className="activity-file-downloads" key={item.id}>
      <Link className="activity-file-download" href={`/?library=${encodeURIComponent(item.knowledge_base!.id)}&view=documents`}>
        <ExternalLink size={15} aria-hidden="true" /><span>打开知识库 · {item.knowledge_base!.name}</span>
      </Link>
    </div>)}
    {previews.length > 0 && <div className="cloud-result-actions" aria-label="成果操作">
      {previews.map(preview => <PreviewResultAction key={preview.id} id={preview.id} name={preview.name} />)}
    </div>}
    {expanded && <div id={regionId} className="activity-details"><ol className="activity-steps" aria-label="处理过程">
      {ordered.map(step => {
        if (step.type === "detail") return <li key={step.id} className="activity-detail">{step.content}</li>;
        const item = itemById.get(step.id);
        return item ? <Motion.li key={item.id} className={`activity-step activity-${item.status}`}
        initial={false} animate={{ opacity: 1 }} transition={{ duration: 0.14 }}>
        <span className="activity-state-icon" aria-hidden="true"><StatusIcon status={item.status} /></span>
        <div className="activity-step-copy">
          <span className="activity-step-title">
            {item.kind === "retrieval" && <Search size={13} aria-hidden="true" />}
            <strong>{item.label}</strong>
            <span className="activity-state-label">{statusLabel[item.status]}</span>
          </span>
          {item.summary && <span className="activity-step-summary">{item.summary}</span>}
          {detailRoot && item.executionId && <ToolReceipt path={`${detailRoot}/${item.executionId}`} />}
        </div>
      </Motion.li> : null; })}
    </ol>{children}</div>}
  </div>;
}

type Receipt = { text: string; missing: boolean; redacted: boolean; truncated: boolean; next_offset: number | null };
function ToolReceipt({ path }: { path: string }) {
  const [open, setOpen] = useState(false);
  const [pages, setPages] = useState<Receipt[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const session = useRef<AbortController | null>(null);
  const reading = useRef<AbortController | null>(null);
  async function load(offset: number, controller: AbortController) {
    if (reading.current === controller) return;
    reading.current = controller;
    try {
      const page = await request<Receipt>(`${path}?offset=${offset}`, { signal: controller.signal, cache: "no-store" });
      if (!controller.signal.aborted) {
        setPages(items => {
          if (offset) return [...items, page];
          const first = items[0];
          return first && first.text === page.text && first.next_offset === page.next_offset &&
            first.redacted === page.redacted && first.missing === page.missing && first.truncated === page.truncated
            ? items : [page];
        });
        setError("");
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setPages([]);
        setError(cause instanceof ApiError && [401, 403, 404].includes(cause.status)
          ? "该操作详情已不可访问" : "暂时无法查看详情");
      }
    } finally {
      if (reading.current === controller) reading.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    session.current = controller;
    setBusy(true);
    void load(0, controller);
    // Recheck currently displayed content without retaining it across sessions.
    const recheck = () => { if (!document.hidden) void load(0, controller); };
    const timer = window.setInterval(recheck, 10_000);
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      session.current = null;
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [open, path]);
  const last = pages.at(-1);
  return <details className="tool-receipt" onToggle={event => {
    setOpen(event.currentTarget.open);
    if (!event.currentTarget.open) { setPages([]); setError(""); }
  }}>
    <summary>查看操作详情</summary>
    {open && <>
      {busy && <span role="status">正在读取…</span>}
      {error && <span role="status">{error}</span>}
      {last?.missing && <span>没有可显示的记录</span>}
      {pages.some(page => page.text) && <pre>{pages.map(page => page.text).join("")}</pre>}
      {last?.redacted && <span>部分内容已隐藏</span>}
      {last?.truncated && <span>显示部分记录</span>}
      {last?.next_offset != null && <button type="button" className="text-button" disabled={busy} onClick={() => {
        if (session.current && last.next_offset != null) {
          setBusy(true);
          void load(last.next_offset, session.current);
        }
      }}>继续查看</button>}
    </>}
  </details>;
}
