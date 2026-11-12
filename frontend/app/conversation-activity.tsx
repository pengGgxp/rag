"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronRight, CircleAlert, CircleDashed, Download, ExternalLink, FileText, Loader2, Search, Square, Wrench } from "lucide-react";
import { Motion } from "../components/ui/motion";
import { API_URL, ApiError, downloadFile, request } from "./api";
import Link from "next/link";
import { ImageTaskActivity, type ImageTaskResult } from "./image-task-result";
import { openPreview, type Preview } from "./cloud-preview";

export type SavedDocument = {
  save_id: string; document_id: string; knowledge_base_id: string; filename: string;
  document_status: string; terminal: boolean; summary: string;
};

export type ConversationActivityItem = {
  id: string;
  label: string;
  summary?: string;
  kind: "retrieval" | "tool" | "compaction";
  status: "waiting" | "running" | "completed" | "failed" | "cancelled" | "uncertain";
  files?: { id: string; path: string; size_bytes: number; version: number }[];
  saved_document?: SavedDocument;
  image_task?: ImageTaskResult;
  preview?: Preview;
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
  cancelled: "已取消",
  uncertain: "结果待确认",
};

function StatusIcon({ status }: { status: ConversationActivityItem["status"] }) {
  if (status === "running") return <Loader2 size={14} className="spin" />;
  if (status === "completed") return <Check size={14} />;
  if (status === "failed" || status === "uncertain") return <CircleAlert size={14} />;
  if (status === "cancelled") return <Square size={12} />;
  return <CircleDashed size={14} />;
}

// Only presentation state lives here. Tool execution and safe summaries belong to the server.
export function ConversationActivity({ items, finished, workspaceId, children }: {
  items: ConversationActivityItem[];
  finished: boolean;
  workspaceId?: string;
  children?: ReactNode;
}) {
  const [expanded, setExpanded] = useState(items.length > 1 && !finished);
  const [userExpanded, setUserExpanded] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const [savedResults, setSavedResults] = useState<Record<string, SavedDocument>>({});
  const [imageResults, setImageResults] = useState<Record<string, ImageTaskResult>>({});
  const updateImage = useCallback((value: ImageTaskResult) => setImageResults(old => ({ ...old, [value.task_id]: value })), []);
  const updateSaved = useCallback((value: SavedDocument) => setSavedResults(old => ({ ...old, [value.save_id]: value })), []);
  items = items.map(item => {
    const image = item.image_task && (imageResults[item.image_task.task_id] || item.image_task);
    if (image) return { ...item, summary: image.summary, status: (!image.terminal ? "running" : image.status === "uncertain" ? "uncertain" : ["failed", "save_failed"].includes(image.status) ? "failed" : image.status === "cancelled" ? "cancelled" : "completed") as ConversationActivityItem["status"] };
    const saved = item.saved_document && (savedResults[item.saved_document.save_id] || item.saved_document);
    return saved ? { ...item, summary: saved.summary,
      status: (saved.document_status === "failed" || saved.document_status === "deleted" ? "failed"
        : saved.terminal ? "completed" : "running") as ConversationActivityItem["status"] } : item;
  });
  useEffect(() => {
    if (!userExpanded) setExpanded(items.length > 1 && !finished);
  }, [finished, userExpanded, items.length]);
  if (!items.length) return null;
  const current = items.find(item => item.status === "running");
  const failed = items.filter(item => item.status === "failed").length;
  const cancelled = items.some(item => item.status === "cancelled");
  const single = items.length === 1 ? items[0] : null;
  const headline = single
    ? `${single.label}${single.saved_document || single.image_task ? ` · ${single.summary}` : single.status === "waiting" ? " · 等待执行" : single.status === "running" ? ` · ${single.summary || "执行中"}` : single.status === "uncertain" ? " · 结果待确认" : single.status === "failed" ? ` · ${single.summary || "未完成"}` : single.status === "cancelled" ? " · 已停止" : single.summary ? ` · ${single.summary}` : " · 已完成"}`
    : current ? current.summary || current.label : finished
    ? cancelled ? `已停止 · ${items.length} 项活动` : failed ? `${items.length} 项活动 · ${failed} 项失败` : `已完成 ${items.length} 项活动`
    : "等待下一步";
  return <div className="conversation-activity">
    <button type="button" className="activity-toggle" aria-expanded={expanded}
      aria-label={`${expanded ? "收起" : "展开"}执行活动`}
      onClick={() => {
        setUserExpanded(!expanded);
        setExpanded(!expanded);
      }}>
      <ChevronRight size={14} className={expanded ? "activity-chevron expanded" : "activity-chevron"} aria-hidden="true" />
      {single ? <span className={`activity-single-icon activity-${single.status}`} aria-hidden="true"><StatusIcon status={single.status} /></span>
        : current ? <Loader2 size={14} className="spin activity-current-icon" aria-hidden="true" />
        : <Wrench size={14} aria-hidden="true" />}
      <span>{headline}</span>
    </button>
    {items.filter(item => item.saved_document).map(item => <SavedDocumentResult key={item.saved_document!.save_id}
      initial={item.saved_document!} onUpdate={updateSaved} showStatus={items.length > 1} />)}
    {items.filter(item => item.image_task).map(item => <ImageTaskActivity key={item.image_task!.task_id} initial={item.image_task!} onUpdate={updateImage} showStatus={items.length > 1} />)}
    {items.filter(item => item.preview).map(item => <div className="activity-file-downloads" key={item.preview!.id}>
      <button type="button" className="activity-file-download" onClick={() => openPreview(item.preview!.id)}><ExternalLink size={15} /><span>{item.preview!.status === "ready" ? "打开预览" : "查看预览状态"} · {item.preview!.name}</span></button>
    </div>)}
    {workspaceId && single?.files?.length ? <div className="activity-file-downloads">
      {single.files.map(file => <a key={`${file.id}-${file.version}`} className="activity-file-download"
        href={`${API_URL}/resources/${file.id}/download`} download
        onClick={event => {
          event.preventDefault();
          setDownloadError("");
          void downloadFile(`/resources/${file.id}/download`, file.path.split("/").at(-1) || "文件")
            .catch(error => setDownloadError(error instanceof Error ? error.message : "下载失败，请重试"));
        }}>
        <Download size={15} aria-hidden="true" /><span>下载 {file.path.split("/").at(-1) || file.path}</span>
      </a>)}
      {downloadError && <span className="activity-file-download-error" role="status">{downloadError}</span>}
    </div> : null}
    {expanded && <><ol className="activity-steps" aria-label="执行活动">
      {items.map(item => <Motion.li key={item.id} className={`activity-step activity-${item.status}`}
        initial={false} animate={{ opacity: 1 }} transition={{ duration: 0.14 }}>
        <span className="activity-state-icon" aria-hidden="true"><StatusIcon status={item.status} /></span>
        <div className="activity-step-copy">
          <span className="activity-step-title">
            {item.kind === "retrieval" && <Search size={13} aria-hidden="true" />}
            <strong>{item.label}</strong>
            <span className="activity-state-label">{statusLabel[item.status]}</span>
          </span>
          {item.summary && <span className="activity-step-summary">{item.summary}</span>}
        </div>
      </Motion.li>)}
    </ol>{children}</>}
    <span className="sr-only" role="status">{headline}</span>
  </div>;
}
