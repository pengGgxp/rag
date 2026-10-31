"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Check, ChevronRight, CircleAlert, CircleDashed, Download, Loader2, Search, Square, Wrench } from "lucide-react";
import { Motion } from "../components/ui/motion";
import { API_URL, downloadFile } from "./api";

export type ConversationActivityItem = {
  id: string;
  label: string;
  summary?: string;
  kind: "retrieval" | "tool" | "compaction";
  status: "waiting" | "running" | "completed" | "failed" | "cancelled" | "uncertain";
  files?: { id: string; path: string; size_bytes: number; version: number }[];
};

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
  useEffect(() => {
    if (!userExpanded) setExpanded(items.length > 1 && !finished);
  }, [finished, userExpanded, items.length]);
  if (!items.length) return null;
  const current = items.find(item => item.status === "running");
  const failed = items.filter(item => item.status === "failed").length;
  const cancelled = items.some(item => item.status === "cancelled");
  const single = items.length === 1 ? items[0] : null;
  const headline = single
    ? `${single.label}${single.status === "waiting" ? " · 等待执行" : single.status === "running" ? " · 执行中" : single.status === "uncertain" ? " · 结果待确认" : single.status === "failed" ? " · 未完成" : single.status === "cancelled" ? " · 已停止" : single.summary ? ` · ${single.summary}` : " · 已完成"}`
    : finished
    ? cancelled ? `已停止 · ${items.length} 项活动` : failed ? `${items.length} 项活动 · ${failed} 项失败` : `已完成 ${items.length} 项活动`
    : current?.label ?? "等待下一步";
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
    {workspaceId && single?.files?.length ? <div className="activity-file-downloads">
      {single.files.map(file => <a key={`${file.id}-${file.version}`} className="activity-file-download"
        href={`${API_URL}/workspaces/${workspaceId}/files/${file.id}/download`} download
        onClick={event => {
          event.preventDefault();
          setDownloadError("");
          void downloadFile(`/workspaces/${workspaceId}/files/${file.id}/download`, file.path.split("/").at(-1) || "文件")
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
