"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Check, FileText, FolderOpen, Loader2, Pause, Play, RefreshCw, Upload, X } from "lucide-react";
import Link from "next/link";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { ApiError, documentFailure, isTemporary, logDocumentFailure, request, retryAfterMs, type DocumentFailure } from "./api";
import { canResumeTransfer, isRestorableUploadTask, requiresFreshUpload, restoreUploadState, uploadIssue } from "./upload-recovery";
import { useAuth } from "./auth";
import { documentRetryLabel, type IngestionRetryProgress } from "./workspace-types";
import { BATCH_COUNT, BATCH_LIMIT, DOCUMENT_ACCEPT, DOCUMENT_LIMIT, droppedDocuments, prepareDocuments, type ImportFile, type ImportIssue } from "./document-import";
import "./uploads.css";

const LIMIT = DOCUMENT_LIMIT;
type Destination = { id: string; name: string; versionOf?: string; filename?: string };
type Task = {
  key: string; uploadId?: string; workspace: string; library: Destination;
  name: string; sourcePath?: string; size: number; modified: number; type: string; partSize: number;
  hashes: Record<number, string>; state: string; transferred: number;
  error?: string; failure?: DocumentFailure; documentId?: string; retryLabel?: string;
  queryPaused?: boolean;
};
type Remote = {
  id: string; state: string; part_size: number; document_id?: string; error?: string;
  document_status?: string; document_error?: string;
  retry_progress?: IngestionRetryProgress | null;
  retry_after?: string | null;
  verify_after?: string | null;
  job_state?: string;
  failure?: DocumentFailure;
  parts: { number: number; size: number; sha256: string }[];
};
type UploadContext = { tasks: Task[]; open: (destination?: Destination) => void };
const Context = createContext<UploadContext>({ tasks: [], open: () => {} });
export const useDocumentUploads = () => useContext(Context);
const labels: Record<string, string> = {
  queued: "等待传输", uploading: "正在传输", paused: "已暂停", verifying: "传输完成 · 正在校验",
  processing: "校验完成 · 文档处理中", ready: "可检索", failed: "上传未完成",
  processing_failed: "文档处理失败", cancelled: "已取消", deleted: "已移至回收站", completing: "等待完成上传",
};
const terminal = ["ready", "cancelled", "deleted", "processing_failed"];
const json = (body: unknown, extraHeaders?: Record<string, string>) => ({
  method: "POST",
  headers: { "Content-Type": "application/json", ...extraHeaders },
  body: JSON.stringify(body),
});
const size = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
const message = (e: unknown) => e instanceof Error ? e.message : "上传失败，请重试";
function failureOf(error: unknown): DocumentFailure | undefined {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== "object") return;
  const detail = error.detail as Partial<DocumentFailure>;
  return typeof detail.code === "string" && typeof detail.stage === "string"
    && typeof detail.message === "string" && typeof detail.retryable === "boolean"
    && typeof detail.request_id === "string" ? detail as DocumentFailure : undefined;
}

async function digest(blob: Blob) {
  const bytes = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}
function put(
  url: string,
  blob: Blob,
  signal: AbortSignal,
  progress: (n: number) => void,
  uploadId: string,
  partNumber: number,
) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const finish = (error?: Error) => {
      signal.removeEventListener("abort", abort);
      if (error && !(error instanceof DOMException && error.name === "AbortError")) {
        logDocumentFailure(`/uploads/${uploadId}/parts/${partNumber}`, {
          status: error instanceof ApiError ? error.status : xhr.status || 0,
          contentType: xhr.getResponseHeader("Content-Type"),
          requestId: xhr.getResponseHeader("X-Request-ID") || undefined,
        });
      }
      error ? reject(error) : resolve();
    };
    xhr.open("PUT", url);
    xhr.timeout = 10 * 60_000;
    xhr.upload.onprogress = event => progress(Math.min(event.loaded, blob.size));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300 && xhr.getResponseHeader("ETag")) {
        finish();
        return;
      }
      const retryable = xhr.status === 0 || xhr.status === 429 || xhr.status >= 500;
      const code = xhr.status === 429 ? "rate_limited"
        : xhr.status >= 500 ? "service_unavailable"
          : xhr.status === 0 ? "network_error" : "transfer_failed";
      const message = xhr.status === 429 ? "请求过于频繁，请稍后重试"
        : xhr.status >= 500 ? "服务暂时不可用，请稍后重试"
          : xhr.status === 0 ? "上传连接中断，请检查网络后重试" : "上传失败，请重试";
      const detail = documentFailure(`/uploads/${uploadId}/parts/${partNumber}`,
        code, message, retryable,
        xhr.getResponseHeader("X-Request-ID") || undefined);
      finish(new ApiError(xhr.status || 0, message, detail,
        retryAfterMs(xhr.getResponseHeader("Retry-After")), detail.request_id));
    };
    xhr.onerror = () => {
      const detail = documentFailure(`/uploads/${uploadId}/parts/${partNumber}`,
        "network_error", "上传连接中断，请检查网络后重试", true);
      finish(new ApiError(0, detail.message, detail, undefined, detail.request_id));
    };
    xhr.ontimeout = () => {
      const detail = documentFailure(`/uploads/${uploadId}/parts/${partNumber}`,
        "request_timeout", "上传超时，请继续上传", true);
      finish(new ApiError(0, detail.message, detail, undefined, detail.request_id));
    };
    xhr.onabort = () => finish(new DOMException("上传已暂停", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) { finish(new DOMException("上传已暂停", "AbortError")); return; }
    // Content-Length is set by the browser and is included in the server signature.
    xhr.send(blob);
  });
}
function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException("已暂停", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

async function retryUploadRequest<T>(send: () => Promise<T>, signal: AbortSignal) {
  let retryWait = 0;
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      return await send();
    } catch (error) {
      if (signal.aborted || !isTemporary(error) || attempt >= 3) throw error;
      const delay = error instanceof ApiError ? error.retryAfter || 0 : 0;
      const nextWait = Math.max(delay, 1_000 * 2 ** attempt + Math.random() * 500);
      if (retryWait + nextWait > 60_000) throw error;
      await wait(nextWait, signal);
      retryWait += nextWait;
    }
  }
}

export function DocumentUploadProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const [tasks, setTasks] = useState<Task[]>([]);
  const current = useRef<Task[]>([]);
  const workspace = useRef(session.workspace_id);
  workspace.current = session.workspace_id;
  const files = useRef(new Map<string, File>());
  const running = useRef(new Map<string, AbortController>());
  const [dialog, setDialog] = useState<{ destination?: Destination } | null>(null);
  const [selected, setSelected] = useState<ImportFile[]>([]);
  const [issues, setIssues] = useState<ImportIssue[]>([]);
  const [preparing, setPreparing] = useState(false);
  const selection = useRef<AbortController | null>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [selectionError, setSelectionError] = useState("");
  const storageKey = `rag-uploads-v1:${session.user.id}`;
  function publish(next: Task[]) {
    current.current = next; setTasks(next);
    try {
      // Never serialize File objects, URLs, credentials or transient errors.
      localStorage.setItem(storageKey, JSON.stringify(next.map(({ key, uploadId, workspace, library, name, sourcePath, size, modified, type, partSize, hashes, state, error, failure, documentId, queryPaused }) =>
        ({ key, uploadId, workspace, library, name, sourcePath, size, modified, type, partSize, hashes, state,
          failure: error ? failure : undefined, documentId, queryPaused }))));
    } catch { /* Uploads remain usable if browser storage is unavailable. */ }
  }
  function patch(key: string, changes: Partial<Task>) {
    publish(current.current.map(task => task.key === key ? { ...task, ...changes } : task));
  }
  useEffect(() => {
    try {
      const saved: Task[] = JSON.parse(localStorage.getItem(storageKey) || "[]");
      const restored = Array.isArray(saved) ? saved.filter(task => isRestorableUploadTask(task, LIMIT))
        .map(task => ({ ...task, hashes: task.hashes || {}, transferred: 0,
          error: uploadIssue(task.state, task.failure?.message, task.retryLabel, task.queryPaused),
          state: restoreUploadState(task.state, task.failure) })) : [];
      current.current = restored; setTasks(restored);
    } catch { /* Invalid local metadata cannot authorize server operations. */ }
    const controllers = running.current;
    return () => { controllers.forEach(controller => controller.abort()); selection.current?.abort(); };
  }, [storageKey]);
  useEffect(() => {
    let changed = false;
    const next = current.current.map(task => {
      if (task.workspace === session.workspace_id || !["uploading", "queued"].includes(task.state)) return task;
      changed = true; running.current.get(task.key)?.abort();
      return { ...task, state: "paused", error: "已切换工作区，返回原工作区后可继续" };
    });
    if (changed) publish(next);
    selection.current?.abort(); setPreparing(false); setDialog(null); setSelected([]); setIssues([]);
    // Upload state remains mounted outside the workspace-keyed page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.workspace_id]);
  function acceptRemote(task: Task, remote: Remote) {
    let state = remote.state;
    if (state === "uploading" && task.state !== "uploading") state = "paused";
    if (state === "completed") state = remote.document_status === "ready" ? "ready"
      : remote.document_status === "failed" ? "processing_failed" : remote.document_status === "deleted" ? "deleted" : "processing";
    const restart = ["expired", "cleaned"].includes(state);
    if (restart) state = "failed";
    if (restart) files.current.delete(task.key);
    const failure = restart ? {
      code: "upload_expired", stage: "upload", message: "上传已过期，请重新选择文件后上传",
      retryable: false, request_id: task.key,
    } : remote.failure;
    if (failure && !failure.retryable) files.current.delete(task.key);
    const retryLabel = state === "verifying" && remote.verify_after
      ? "校验中 · 正在自动重试"
      : documentRetryLabel(remote.document_status, remote.job_state, remote.retry_after, remote.retry_progress);
    patch(task.key, { state, documentId: remote.document_id, failure, queryPaused: false, retryLabel,
      error: uploadIssue(state, failure?.message || remote.error || remote.document_error || undefined, retryLabel),
      ...(restart ? { uploadId: undefined, hashes: {}, transferred: 0 } : {}),
      ...(["verifying", "processing", "ready", "processing_failed"].includes(state) ? { transferred: task.size } : {}) });
    if (["verifying", "processing", "ready", "processing_failed", "deleted", "cancelled"].includes(state)) files.current.delete(task.key);
    if (remote.document_id && !task.documentId)
      window.dispatchEvent(new CustomEvent("rag-upload-admitted", { detail: { library: task.library.id } }));
  }
  const pollInFlight = useRef(new Set<string>());
  async function poll(taskKey?: string) {
    const candidates = current.current.filter(task => task.workspace === workspace.current && task.uploadId
      && ["verifying", "processing"].includes(task.state)
      && (!taskKey || task.key === taskKey)
      && (!task.queryPaused || task.key === taskKey)
      && !pollInFlight.current.has(task.key)
      && !running.current.has(task.key));
    await Promise.all(candidates.map(async task => {
      pollInFlight.current.add(task.key);
      try {
        const remote = await retryUploadRequest(() => request<Remote>(`/uploads/${task.uploadId}`), new AbortController().signal);
        const latest = current.current.find(t => t.key === task.key);
        if (latest && remote && latest.workspace === workspace.current && ["verifying", "processing"].includes(latest.state)) acceptRemote(latest, remote);
      } catch (e) {
        const expired = e instanceof ApiError && e.status === 410;
        if (expired) files.current.delete(task.key);
        const queryFailure = expired ? {
          code: "upload_expired",
          stage: "upload",
          message: "上传已过期，请重新选择文件后上传",
          retryable: false,
          request_id: e instanceof ApiError ? e.requestId || task.key : task.key,
        } : (!isTemporary(e) && failureOf(e)) || documentFailure(
          `/uploads/${task.uploadId}`,
          "query_failed",
          "暂时无法获取上传进度，请点击重试查询",
          isTemporary(e),
          e instanceof ApiError ? e.requestId : undefined,
        );
        if (workspace.current === task.workspace) patch(task.key, {
          error: queryFailure.message,
          failure: queryFailure,
          queryPaused: !expired,
          ...(expired ? { state: "failed", uploadId: undefined, hashes: {}, transferred: 0 } : {}),
        });
      } finally {
        pollInFlight.current.delete(task.key);
      }
    }));
  }
  useEffect(() => {
    const timer = setInterval(() => void poll(), 2500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.workspace_id]);

  async function run(task: Task) {
    const file = files.current.get(task.key);
    if (!file || running.current.has(task.key) || workspace.current !== task.workspace) return;
    const controller = new AbortController();
    running.current.set(task.key, controller);
    const signal = controller.signal;
    patch(task.key, { state: "uploading", error: undefined, retryLabel: undefined });
    const sending = new Map<number, number>();
    let completing = false;
    try {
      let remote = task.uploadId ? await request<Remote>(`/uploads/${task.uploadId}`, { signal })
        : await retryUploadRequest(() => request<Remote>(`/knowledge-bases/${task.library.id}/uploads`, json({
          filename: file.name, size_bytes: file.size, content_type: file.type || "application/octet-stream",
          version_of: task.library.versionOf || null,
        }, { "Idempotency-Key": task.key })), signal);
      task = { ...task, uploadId: remote.id, partSize: remote.part_size };
      patch(task.key, { uploadId: remote.id, partSize: remote.part_size });
      if (signal.aborted) {
        if (current.current.find(t => t.key === task.key)?.state === "cancelled")
          await request(`/uploads/${remote.id}`, { method: "DELETE" });
        return;
      }
      if (remote.state === "completing") {
        completing = true;
        remote = await request<Remote>(`/uploads/${remote.id}/complete`, { ...json({}), signal });
        signal.throwIfAborted();
        acceptRemote(task, remote); return;
      }
      if (remote.state === "failed" && remote.failure?.retryable && remote.failure.stage === "verification") {
        completing = true;
        remote = await request<Remote>(`/uploads/${remote.id}/complete`, { ...json({}), signal });
        signal.throwIfAborted();
        acceptRemote(task, remote); return;
      }
      if (remote.state !== "uploading") { acceptRemote(task, remote); return; }
      const uploaded = new Set<number>();
      let completedBytes = 0;
      for (const part of remote.parts) {
        const hash = await digest(file.slice((part.number - 1) * task.partSize, (part.number - 1) * task.partSize + part.size));
        signal.throwIfAborted();
        if (hash !== part.sha256) {
          const detail = documentFailure(`/uploads/${remote.id}/parts/${part.number}`,
            "integrity_mismatch", "所选文件与已上传的分片不一致，请重新选择文件上传", false);
          throw new ApiError(409, detail.message, detail);
        }
        uploaded.add(part.number); completedBytes += part.size;
      }
      patch(task.key, { transferred: completedBytes });
      const numbers = Array.from({ length: Math.ceil(file.size / task.partSize) }, (_, i) => i + 1).filter(n => !uploaded.has(n));
      const progress = () => patch(task.key, { transferred: completedBytes + Array.from(sending.values()).reduce((a, b) => a + b, 0) });
      let cursor = 0;
      const worker = async () => {
        while (cursor < numbers.length) {
          signal.throwIfAborted();
          const number = numbers[cursor++];
          const blob = file.slice((number - 1) * task.partSize, Math.min(number * task.partSize, file.size));
          const hash = await digest(blob);
          signal.throwIfAborted();
          const latest = current.current.find(t => t.key === task.key)!;
          patch(task.key, { hashes: { ...latest.hashes, [number]: hash } });
          let retryWait = 0;
          for (let attempt = 0; ; attempt++) {
            try {
              const signed = await request<{ url: string }>(`/uploads/${remote.id}/parts/${number}`, { ...json({ sha256: hash }), signal });
              signal.throwIfAborted();
              await put(signed.url, blob, signal, n => { sending.set(number, n); progress(); }, remote.id, number);
              break;
            } catch (e) {
              if (signal.aborted || attempt >= 3 || !isTemporary(e)) throw e;
              sending.delete(number); progress();
              const retryAfter = e instanceof ApiError ? e.retryAfter || 0 : 0;
              const nextWait = Math.max(retryAfter, 1_000 * 2 ** attempt + Math.random() * 500);
              if (retryWait + nextWait > 60_000) throw e;
              await wait(nextWait, signal);
              retryWait += nextWait;
            }
          }
          sending.delete(number); completedBytes += blob.size; progress();
        }
      };
      let failure: unknown;
      await Promise.all(Array.from({ length: Math.min(3, numbers.length) }, async () => {
        try { await worker(); } catch (e) { if (!failure) failure = e; controller.abort(); }
      }));
      if (failure) {
        if (!(failure instanceof DOMException && failure.name === "AbortError"))
          patch(task.key, { state: "failed", error: message(failure), failure: failureOf(failure) });
        return;
      }
      signal.throwIfAborted();
      patch(task.key, { state: "verifying", transferred: file.size });
      completing = true;
      remote = await request<Remote>(`/uploads/${remote.id}/complete`, { ...json({}), signal });
      signal.throwIfAborted();
      acceptRemote(task, remote);
    } catch (e) {
      if (!signal.aborted) {
        // Stop sibling parts before exposing Retry.
        controller.abort();
        const expired = e instanceof ApiError && e.status === 410;
        if (expired) files.current.delete(task.key);
        const reconciling = completing && isTemporary(e);
        patch(task.key, { state: reconciling ? "verifying" : "failed",
          error: reconciling ? undefined : message(e), failure: failureOf(e),
          retryLabel: reconciling ? "正在确认上传状态" : undefined });
      }
    } finally {
      running.current.delete(task.key);
      setTasks([...current.current]);
    }
  }
  useEffect(() => {
    if (running.current.size) return;
    if (tasks.filter(t => t.workspace === workspace.current && t.state === "verifying").length >= 10) return;
    const next = tasks.find(t => t.workspace === workspace.current && t.state === "queued" && files.current.has(t.key));
    if (next) void run(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasks]);
  function resume(task: Task, file?: File) {
    if (file) {
      if (requiresFreshUpload(task.failure?.code)) {
        void startFreshUpload(task, file);
        return;
      }
      if (file.name !== task.name || file.size !== task.size) {
        patch(task.key, { error: "请选择原文件，名称及大小必须一致；继续前还会核对已上传分片" }); return;
      }
      files.current.set(task.key, file);
    }
    patch(task.key, { state: "queued", error: undefined, queryPaused: false });
  }
  async function startFreshUpload(task: Task, file: File) {
    if (task.uploadId) {
      try { await request(`/uploads/${task.uploadId}`, { method: "DELETE" }); }
      catch { /* A sealed or expired session may already be unavailable for cancellation. */ }
    }
    const key = crypto.randomUUID();
    files.current.delete(task.key);
    files.current.set(key, file);
    publish(current.current.map(item => item.key === task.key
      ? { ...item, key, uploadId: undefined, hashes: {}, transferred: 0, documentId: undefined,
        name: file.name, size: file.size, sourcePath: undefined,
        modified: file.lastModified, type: file.type, state: "queued",
        error: undefined, failure: undefined, queryPaused: false }
      : item));
  }
  async function retryVerification(task: Task) {
    if (!task.uploadId || running.current.has(task.key) || pollInFlight.current.has(task.key)) return;
    const controller = new AbortController();
    running.current.set(task.key, controller);
    patch(task.key, { state: "verifying", error: undefined, retryLabel: undefined, queryPaused: false });
    try {
      const remote = await request<Remote>(`/uploads/${task.uploadId}/complete`, { ...json({}), signal: controller.signal });
      controller.signal.throwIfAborted();
      acceptRemote(task, remote);
    } catch (error) {
      if (!controller.signal.aborted) {
        const reconciling = isTemporary(error);
        patch(task.key, { state: reconciling ? "verifying" : "failed",
          failure: failureOf(error) || task.failure, error: reconciling ? undefined : message(error),
          retryLabel: reconciling ? "正在确认上传状态" : undefined });
      }
    } finally {
      running.current.delete(task.key);
      setTasks([...current.current]);
    }
  }
  async function cancel(task: Task) {
    running.current.get(task.key)?.abort();
    patch(task.key, { state: "paused", error: undefined });
    try {
      if (task.uploadId) await request(`/uploads/${task.uploadId}`, { method: "DELETE" });
      patch(task.key, { state: "cancelled" }); files.current.delete(task.key);
    } catch (e) { patch(task.key, { state: "failed", error: message(e) }); }
  }
  async function choose(input: readonly File[] | ((signal: AbortSignal) => Promise<File[]>)) {
    selection.current?.abort();
    const controller = new AbortController(); selection.current = controller;
    setPreparing(true); setSelectionError("");
    try {
      const candidates = typeof input === "function" ? await input(controller.signal) : input;
      const result = await prepareDocuments(candidates, controller.signal, Boolean(dialog?.destination?.versionOf));
      controller.signal.throwIfAborted();
      const merged = dialog?.destination?.versionOf ? [] : [...selected];
      let bytes = merged.reduce((sum, item) => sum + item.file.size, 0);
      for (const item of result.files) {
        if (merged.some(old => old.path === item.path && old.file.size === item.file.size && old.file.lastModified === item.file.lastModified)) {
          result.issues.push({ path: item.path, reason: "已在待上传列表中" }); continue;
        }
        if (merged.length >= BATCH_COUNT || bytes + item.file.size > BATCH_LIMIT) {
          result.issues.push({ path: item.path, reason: "本批超过 200 个文档或 500 MiB，请分批上传" }); continue;
        }
        merged.push(item); bytes += item.file.size;
      }
      setSelected(merged); setIssues(result.issues);
      if (!merged.length && !result.issues.length) setSelectionError("没有找到支持的文档，请选择其他文件");
    } catch (e) {
      if (!controller.signal.aborted) setSelectionError(message(e));
    } finally { if (selection.current === controller) setPreparing(false); }
  }
  function closeDialog() {
    selection.current?.abort(); setPreparing(false); setDialog(null); setSelected([]); setIssues([]);
  }
  function enqueue() {
    if (!dialog?.destination) return;
    const next = selected.map(({ file, path }) => {
      const task: Task = { key: crypto.randomUUID(), workspace: session.workspace_id, library: dialog.destination!,
        name: file.name, sourcePath: path, size: file.size, modified: file.lastModified, type: file.type,
        partSize: 8 * 1024 * 1024, hashes: {}, state: "queued", transferred: 0 };
      files.current.set(task.key, file); return task;
    });
    publish([...current.current, ...next]); setSelected([]); setIssues([]);
  }
  const visible = tasks.filter(task => task.workspace === session.workspace_id);
  const open = (destination?: Destination) => { selection.current?.abort(); setPreparing(false); setDialog({ destination }); setSelected([]); setIssues([]); setSelectionError(""); };
  return <Context.Provider value={{ tasks: visible, open }}>
    {children}
    {dialog && <Dialog title={dialog.destination?.versionOf ? `上传 ${dialog.destination.filename} 的新版本` : dialog.destination ? "上传文档" : "上传任务"}
      onClose={closeDialog} restoreFocusSelector={dialog.destination ? "[data-upload-trigger]" : "[data-upload-tasks]"}>
      <div className="upload-window">
        {dialog.destination && <>
          <div className="upload-destination"><BookOpen size={15} /><span>{dialog.destination.name}</span></div>
          <label className={`dropzone ${selected.length ? "has-file" : ""}`} onDragOver={e => e.preventDefault()} onDrop={e => {
            e.preventDefault(); if (preparing) return;
            const transfer = e.dataTransfer;
            void choose(signal => droppedDocuments(transfer, signal));
          }}>
            <input type="file" aria-label="选择文件" disabled={preparing} multiple={!dialog.destination.versionOf}
              accept={DOCUMENT_ACCEPT + (dialog.destination.versionOf ? "" : ",.zip")}
              onChange={e => { void choose(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
            <span className="upload-symbol"><Upload size={26} /></span>
            <strong>{preparing ? "正在整理文件" : selected.length ? `待上传 ${selected.length} 个文档` : "选择文件或拖拽到此处"}</strong>
            <span>PDF、DOCX、MD、TXT、HTML、CSV、XLSX{!dialog.destination.versionOf && "、ZIP"}</span>
            <small>单文件最大 200 MiB{!dialog.destination.versionOf && " · 每批最多 200 个文档、500 MiB"}</small>
          </label>
          {!dialog.destination.versionOf && <div className="upload-selection-actions">
            <input type="file" ref={folderInput} aria-label="选择文件夹" className="sr-only" tabIndex={-1} disabled={preparing} multiple
              {...{ webkitdirectory: "", directory: "" }} onChange={e => { void choose(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
            <button className="secondary" disabled={preparing} onClick={() => folderInput.current?.click()}><FolderOpen size={16} />选择文件夹</button>
            {!!selected.length && <button className="secondary" disabled={preparing} onClick={() => { setSelected([]); setIssues([]); }}>清空待上传</button>}
          </div>}
          {preparing && <p className="upload-preparing" role="status"><Loader2 size={16} className="spin" />正在检查文档和压缩包</p>}
          {!!selected.length && <ul className="upload-file-list">{selected.map(({ file, path }, i) => <li key={i}>
            <FileText size={14} /><span title={path}>{path}</span><small>{size(file.size)}</small>
            <button className="icon-button" disabled={preparing} title="移除此文件" aria-label={`移除待上传的${path}`} onClick={() => setSelected(selected.filter((_, index) => i !== index))}><X size={14} /></button>
          </li>)}</ul>}
          {!!issues.length && <details className="upload-import-issues" open={!selected.length}><summary>未加入 {issues.length} 个项目</summary>
            <ul>{issues.map((issue, i) => <li key={i}><span>{issue.path}</span><small>{issue.reason}</small></li>)}</ul>
          </details>}
          {selectionError && <p className="form-error" role="alert">{selectionError}</p>}
          <div className="upload-start"><button className="primary" disabled={!selected.length || preparing} onClick={enqueue}><Upload size={15} />上传{selected.length ? ` ${selected.length} 个文档` : ""}</button></div>
        </>}
        <ul className="transfer-list" aria-label="上传任务">
          {visible.map(task => <li key={task.key}>
            <div className="transfer-heading"><FileText size={17} /><strong title={task.sourcePath || task.name}>{task.sourcePath || task.name}</strong><span>{size(task.size)}</span></div>
            <div className="transfer-state" aria-live="polite">
              {task.state === "ready" ? <Check size={14} /> : !task.queryPaused && ["uploading", "verifying", "processing"].includes(task.state) ? <Loader2 size={14} className="spin" /> : null}
              <span>{task.queryPaused ? "进度查询已暂停" : task.retryLabel
                || (task.state === "uploading" && !task.uploadId ? "正在准备上传" : labels[task.state]) || "等待恢复"}</span>
              {task.state === "uploading" && <span>{Math.floor(Math.min(task.transferred, task.size) / task.size * 100)}%</span>}
              <small>{task.library.name}</small>
            </div>
            {task.state === "uploading" && <progress max={task.size} value={Math.min(task.transferred, task.size)} aria-label={`${task.name}传输进度`} />}
            {task.error && <p className="form-error" role="alert">{task.error}</p>}
            <div className="transfer-actions">
              {["uploading", "queued"].includes(task.state) && <Tooltip content="暂停传输"><button className="icon-button" aria-label={`暂停${task.name}`} onClick={() => {
                patch(task.key, { state: "paused" }); running.current.get(task.key)?.abort();
              }}><Pause size={16} /></button></Tooltip>}
              {task.state === "completing" || task.state === "failed" && task.failure?.retryable && task.failure.stage === "verification"
                ? <button className="secondary" onClick={() => void retryVerification(task)}><RefreshCw size={14} />重试校验</button>
                : ["paused", "failed"].includes(task.state) && (canResumeTransfer(task.failure) && files.current.has(task.key)
                ? <button className="secondary" onClick={() => resume(task)}><Play size={14} />继续上传</button>
                : (canResumeTransfer(task.failure) || requiresFreshUpload(task.failure?.code)) && <label className="secondary transfer-reselect"><Upload size={14} />{requiresFreshUpload(task.failure?.code) ? "重新选择文件上传" : "重选原文件继续"}<input type="file" aria-label={`重新选择${task.name}`} accept={DOCUMENT_ACCEPT} onChange={e => { if (e.target.files?.[0]) resume(task, e.target.files[0]); e.target.value = ""; }} /></label>)}
              {["verifying", "processing"].includes(task.state) && (task.queryPaused || task.error && !task.failure?.retryable) && <button className="secondary" onClick={() => void poll(task.key)}><RefreshCw size={14} />重试查询</button>}
              {task.documentId && <Link className="secondary" href={`/?library=${task.library.id}`} onClick={() => setDialog(null)}>查看文档</Link>}
              {!terminal.includes(task.state) && task.state !== "processing" && <Tooltip content="取消上传"><button className="icon-button" aria-label={`取消${task.name}`} onClick={() => void cancel(task)}><X size={16} /></button></Tooltip>}
              {terminal.includes(task.state) && <Tooltip content="移除此任务记录"><button className="icon-button" aria-label={`移除${task.name}记录`} onClick={() => publish(current.current.filter(t => t.key !== task.key))}><X size={16} /></button></Tooltip>}
            </div>
          </li>)}
        </ul>
        {!visible.length && !dialog.destination && <p className="upload-empty">没有上传任务</p>}
      </div>
    </Dialog>}
  </Context.Provider>;
}

export function UploadTaskEntry({ onOpen }: { onOpen: () => void }) {
  const { tasks, open } = useDocumentUploads();
  if (!tasks.length) return null;
  const pending = tasks.filter(t => !terminal.includes(t.state)).length;
  return <button className="navigation-item upload-task-entry" data-upload-tasks onClick={() => { onOpen(); open(); }}>
    <Upload size={18} /><span>上传任务</span><small>{pending || tasks.length}</small>
  </button>;
}
