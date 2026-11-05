"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Download, Eye, Loader2, RotateCcw, Square } from "lucide-react";
import { API_URL, ApiError, downloadFile, request, sessionHeaders } from "./api";
import { json } from "./admin/admin-ui";
import { Modal } from "./modal";
import "./image-tasks.css";

export type ImageTaskResult = {
  task_id: string; status: string; terminal: boolean; summary: string; credits: string;
  cancel_requested: boolean; files: { id: string; path: string; version: number }[];
};

export function ImageTaskActivity({ initial, onUpdate, showStatus = true }: { initial: ImageTaskResult; onUpdate?: (value: ImageTaskResult) => void; showStatus?: boolean }) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview.url); }, [preview]);
  const element = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false, visible = false, polling = false, terminal = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      if (disposed || !visible || document.hidden || terminal || polling) return;
      polling = true;
      try {
        const next = await request<ImageTaskResult>(`/image-tasks/${initial.task_id}`);
        if (!disposed) { setValue(next); onUpdate?.(next); setError(""); terminal = next.terminal; }
      } catch (cause) {
        if (!disposed) {
          terminal = cause instanceof ApiError && [401, 403, 404].includes(cause.status);
          setError(terminal ? "图片任务暂不可访问" : "进度更新失败，请稍后重试");
        }
      } finally { polling = false; if (!disposed && !terminal) timer = setTimeout(refresh, 4000); }
    }
    const resume = () => { if (timer) clearTimeout(timer); void refresh(); };
    const observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; if (visible) resume(); });
    if (element.current) observer.observe(element.current);
    document.addEventListener("visibilitychange", resume);
    return () => { disposed = true; if (timer) clearTimeout(timer); observer.disconnect(); document.removeEventListener("visibilitychange", resume); };
  }, [initial.task_id, value.status === "save_failed", onUpdate]);
  async function act(action: "cancel" | "retry-save") {
    setBusy(true); setError("");
    try {
      const next = await request<ImageTaskResult>(`/image-tasks/${value.task_id}/${action}`, json({}));
      setValue(next); onUpdate?.(next);
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : "操作未完成，请重试"); }
    finally { setBusy(false); }
  }
  return <div ref={element} className="image-task-result">
    {showStatus && <span className="activity-step-summary" role="status">{!value.terminal && <Loader2 size={14} className="spin" />}{value.summary}</span>}
    {!value.terminal && !value.cancel_requested && <button type="button" className="secondary" disabled={busy} onClick={() => void act("cancel")}><Square size={14} />停止</button>}
    {value.status === "save_failed" && <button type="button" className="secondary" disabled={busy} onClick={() => void act("retry-save")}><RotateCcw size={14} />重试保存</button>}
    {value.files.map(file => <span key={file.id} className="image-task-files"><button type="button" className="activity-file-download" disabled={busy} onClick={async () => {
      setBusy(true); setError("");
      try {
        const response = await fetch(`${API_URL}/resources/${file.id}/download?version=${file.version}`, { credentials: "include", headers: sessionHeaders() });
        if (!response.ok) throw new Error("预览失败，请确认下载额度后重试");
        const blob = await response.blob();
        if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) throw new Error("此文件暂不支持图片预览");
        setPreview({ url: URL.createObjectURL(blob), name: file.path.split("/").at(-1) || "图片" });
      } catch (cause) { setError(cause instanceof Error ? cause.message : "预览失败，请重试"); }
      finally { setBusy(false); }
    }}><Eye size={15} />预览</button><a className="activity-file-download" href={`${API_URL}/resources/${file.id}/download?version=${file.version}`} onClick={event => {
      event.preventDefault(); void downloadFile(`/resources/${file.id}/download?version=${file.version}`, file.path.split("/").at(-1) || "图片").catch(cause => setError(cause instanceof Error ? cause.message : "下载失败，请重试"));
    }}><Download size={15} /><span>{file.path.split("/").at(-1)}</span></a></span>)}
    {!!value.files.length && <Link className="activity-file-download" href="/resources">查看资源库</Link>}
    {error && <span className="activity-file-download-error" role="alert">{error}</span>}
    {preview && <Modal title={preview.name} wide onClose={() => setPreview(null)}><img className="image-task-preview" src={preview.url} alt={preview.name} /></Modal>}
  </div>;
}
