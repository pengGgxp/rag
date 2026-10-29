"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Check, FileText, Loader2, Pause, Play, RefreshCw, Upload, X } from "lucide-react";
import Link from "next/link";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { ApiError, request } from "./api";
import { useAuth } from "./auth";
import "./uploads.css";

const LIMIT = 200 * 1024 * 1024;
type Destination = { id: string; name: string; versionOf?: string; filename?: string };
type Task = {
  key: string; uploadId?: string; workspace: string; library: Destination;
  name: string; size: number; modified: number; type: string; partSize: number;
  hashes: Record<number, string>; state: string; transferred: number;
  error?: string; documentId?: string;
};
type Remote = {
  id: string; state: string; part_size: number; document_id?: string; error?: string;
  document_status?: string; document_error?: string;
  parts: { number: number; size: number; sha256: string }[];
};
type UploadContext = { tasks: Task[]; open: (destination?: Destination) => void };
const Context = createContext<UploadContext>({ tasks: [], open: () => {} });
export const useDocumentUploads = () => useContext(Context);
const labels: Record<string, string> = {
  queued: "等待传输", uploading: "正在传输", paused: "已暂停", verifying: "传输完成 · 正在校验",
  processing: "校验完成 · 文档处理中", ready: "可检索", failed: "上传未完成",
  processing_failed: "文档处理失败", cancelled: "已取消", deleted: "已移至回收站",
};
const terminal = ["ready", "cancelled", "deleted", "processing_failed"];
const json = (body: unknown) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const size = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
const message = (e: unknown) => e instanceof Error ? e.message : "上传失败，请重试";

async function digest(blob: Blob) {
  const bytes = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}
function put(url: string, blob: Blob, signal: AbortSignal, progress: (n: number) => void) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const finish = (error?: Error) => { signal.removeEventListener("abort", abort); error ? reject(error) : resolve(); };
    xhr.open("PUT", url);
    xhr.timeout = 10 * 60_000;
    xhr.upload.onprogress = event => progress(Math.min(event.loaded, blob.size));
    xhr.onload = () => finish(xhr.status >= 200 && xhr.status < 300 && xhr.getResponseHeader("ETag")
      ? undefined : new Error("上传失败，请重试；若仍失败，请联系管理员"));
    xhr.onerror = () => finish(new Error("上传连接中断，请检查网络后重试"));
    xhr.ontimeout = () => finish(new Error("上传超时，请继续上传"));
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

export function DocumentUploadProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const [tasks, setTasks] = useState<Task[]>([]);
  const current = useRef<Task[]>([]);
  const workspace = useRef(session.workspace_id);
  workspace.current = session.workspace_id;
  const files = useRef(new Map<string, File>());
  const running = useRef(new Map<string, AbortController>());
  const [dialog, setDialog] = useState<{ destination?: Destination } | null>(null);
  const [selected, setSelected] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState("");
  const storageKey = `rag-uploads-v1:${session.user.id}`;
  function publish(next: Task[]) {
    current.current = next; setTasks(next);
    try {
      // Never serialize File objects, URLs, credentials or transient errors.
      localStorage.setItem(storageKey, JSON.stringify(next.map(({ key, uploadId, workspace, library, name, size, modified, type, partSize, hashes, state, documentId }) =>
        ({ key, uploadId, workspace, library, name, size, modified, type, partSize, hashes, state, documentId }))));
    } catch { /* Uploads remain usable if browser storage is unavailable. */ }
  }
  function patch(key: string, changes: Partial<Task>) {
    publish(current.current.map(task => task.key === key ? { ...task, ...changes } : task));
  }
  useEffect(() => {
    try {
      const saved: Task[] = JSON.parse(localStorage.getItem(storageKey) || "[]");
      const restored = Array.isArray(saved) ? saved.filter(task => typeof task.uploadId === "string" && /^[0-9a-f-]{36}$/.test(task.uploadId)
        && typeof task.name === "string" && task.size > 0 && task.size <= LIMIT && typeof task.library?.id === "string")
        .map(task => ({ ...task, hashes: task.hashes || {}, transferred: 0,
          state: ["uploading", "queued", "failed"].includes(task.state) ? "paused" : task.state })) : [];
      current.current = restored; setTasks(restored);
    } catch { /* Invalid local metadata cannot authorize server operations. */ }
    const controllers = running.current;
    return () => { controllers.forEach(controller => controller.abort()); };
  }, [storageKey]);
  useEffect(() => {
    let changed = false;
    const next = current.current.map(task => {
      if (task.workspace === session.workspace_id || !["uploading", "queued"].includes(task.state)) return task;
      changed = true; running.current.get(task.key)?.abort();
      return { ...task, state: "paused", error: "已切换工作区，返回原工作区后可继续" };
    });
    if (changed) publish(next);
    setDialog(null); setSelected([]);
    // Upload state remains mounted outside the workspace-keyed page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.workspace_id]);
  function acceptRemote(task: Task, remote: Remote) {
    let state = remote.state;
    if (state === "completed") state = remote.document_status === "ready" ? "ready"
      : remote.document_status === "failed" ? "processing_failed" : remote.document_status === "deleted" ? "deleted" : "processing";
    const restart = ["failed", "expired", "cleaned"].includes(state);
    if (restart) state = "failed";
    patch(task.key, { state, documentId: remote.document_id, error: remote.error || remote.document_error || undefined,
      ...(restart ? { uploadId: undefined, hashes: {}, transferred: 0 } : {}),
      ...(["verifying", "processing", "ready", "processing_failed"].includes(state) ? { transferred: task.size } : {}) });
    if (remote.document_id && !task.documentId)
      window.dispatchEvent(new CustomEvent("rag-upload-admitted", { detail: { library: task.library.id } }));
  }
  async function poll() {
    await Promise.all(current.current.filter(task => task.workspace === workspace.current && task.uploadId
      && ["verifying", "processing"].includes(task.state)).map(async task => {
      try {
        const remote = await request<Remote>(`/uploads/${task.uploadId}`);
        const latest = current.current.find(t => t.key === task.key);
        if (latest && latest.workspace === workspace.current && ["verifying", "processing"].includes(latest.state)) acceptRemote(latest, remote);
      } catch (e) {
        if (workspace.current === task.workspace) patch(task.key, { error: message(e),
          ...(e instanceof ApiError && [403, 404, 410].includes(e.status) ? { state: "failed" } : {}) });
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
    patch(task.key, { state: "uploading", error: undefined });
    const sending = new Map<number, number>();
    try {
      let remote = task.uploadId ? await request<Remote>(`/uploads/${task.uploadId}`, { signal })
        : await request<Remote>(`/knowledge-bases/${task.library.id}/uploads`, json({
          filename: file.name, size_bytes: file.size, content_type: file.type || "application/octet-stream", version_of: task.library.versionOf || null,
        }));
      task = { ...task, uploadId: remote.id, partSize: remote.part_size };
      patch(task.key, { uploadId: remote.id, partSize: remote.part_size });
      if (signal.aborted) {
        if (current.current.find(t => t.key === task.key)?.state === "cancelled")
          await request(`/uploads/${remote.id}`, { method: "DELETE" });
        return;
      }
      if (remote.state === "completing") {
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
        if (hash !== part.sha256) throw new Error("原文件与已上传分片不一致，请取消此任务后重新上传");
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
          for (let attempt = 0; ; attempt++) {
            try {
              const signed = await request<{ url: string }>(`/uploads/${remote.id}/parts/${number}`, { ...json({ sha256: hash }), signal });
              signal.throwIfAborted();
              await put(signed.url, blob, signal, n => { sending.set(number, n); progress(); });
              break;
            } catch (e) {
              if (signal.aborted || attempt >= 3 || e instanceof ApiError && e.status < 500) throw e;
              sending.delete(number); progress();
              await wait(500 * 2 ** attempt, signal);
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
          patch(task.key, { state: "failed", error: message(failure) });
        return;
      }
      signal.throwIfAborted();
      patch(task.key, { state: "verifying", transferred: file.size });
      remote = await request<Remote>(`/uploads/${remote.id}/complete`, { ...json({}), signal });
      signal.throwIfAborted();
      acceptRemote(task, remote);
    } catch (e) {
      if (!signal.aborted) {
        // Stop sibling parts before exposing Retry.
        controller.abort();
        patch(task.key, { state: "failed", error: message(e) });
      }
    } finally {
      running.current.delete(task.key);
      setTasks([...current.current]);
    }
  }
  useEffect(() => {
    if (running.current.size) return;
    const next = tasks.find(t => t.workspace === workspace.current && t.state === "queued" && files.current.has(t.key));
    if (next) void run(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasks]);
  function resume(task: Task, file?: File) {
    if (file) {
      if (file.name !== task.name || file.size !== task.size) {
        patch(task.key, { error: "请选择原文件，名称及大小必须一致；继续前还会核对已上传分片" }); return;
      }
      files.current.set(task.key, file);
    }
    patch(task.key, { state: "queued", error: undefined });
  }
  async function cancel(task: Task) {
    running.current.get(task.key)?.abort();
    patch(task.key, { state: "paused", error: undefined });
    try {
      if (task.uploadId) await request(`/uploads/${task.uploadId}`, { method: "DELETE" });
      patch(task.key, { state: "cancelled" }); files.current.delete(task.key);
    } catch (e) { patch(task.key, { state: "failed", error: message(e) }); }
  }
  function choose(list: FileList | null) {
    const candidates = Array.from(list ?? []);
    setSelectionError(candidates.some(file => file.size > LIMIT || file.size === 0) ? "文件不能为空，单个文件最大 200 MiB" : "");
    setSelected(candidates.filter(file => file.size > 0 && file.size <= LIMIT).slice(0, dialog?.destination?.versionOf ? 1 : 20));
  }
  function enqueue() {
    if (!dialog?.destination) return;
    const next = selected.map(file => {
      const task: Task = { key: crypto.randomUUID(), workspace: session.workspace_id, library: dialog.destination!,
        name: file.name, size: file.size, modified: file.lastModified, type: file.type,
        partSize: 8 * 1024 * 1024, hashes: {}, state: "queued", transferred: 0 };
      files.current.set(task.key, file); return task;
    });
    publish([...current.current, ...next]); setSelected([]);
  }
  const visible = tasks.filter(task => task.workspace === session.workspace_id);
  const open = (destination?: Destination) => { setDialog({ destination }); setSelected([]); setSelectionError(""); };
  return <Context.Provider value={{ tasks: visible, open }}>
    {children}
    {dialog && <Dialog title={dialog.destination?.versionOf ? `上传 ${dialog.destination.filename} 的新版本` : dialog.destination ? "上传文档" : "上传任务"}
      onClose={() => setDialog(null)} restoreFocusSelector={dialog.destination ? "[data-upload-trigger]" : "[data-upload-tasks]"}>
      <div className="upload-window">
        {dialog.destination && <>
          <div className="upload-destination"><BookOpen size={15} /><span>{dialog.destination.name}</span></div>
          <label className={`dropzone ${selected.length ? "has-file" : ""}`} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); choose(e.dataTransfer.files); }}>
            <input type="file" aria-label="选择文件" multiple={!dialog.destination.versionOf} accept=".pdf,.docx,.md,.txt,.html,.htm,.csv,.xlsx" onChange={e => choose(e.target.files)} />
            <span className="upload-symbol"><Upload size={26} /></span>
            <strong>{selected.length ? `已选择 ${selected.length} 个文件` : "选择文件或拖拽到此处"}</strong>
            <span>PDF、DOCX、MD、TXT、HTML、CSV、XLSX</span><small>单文件最大 200 MiB</small>
          </label>
          {!!selected.length && <ul className="upload-file-list">{selected.map((file, i) => <li key={i}><FileText size={14} /><span>{file.name}</span><small>{size(file.size)}</small></li>)}</ul>}
          {selectionError && <p className="form-error" role="alert">{selectionError}</p>}
          <div className="upload-start"><button className="primary" disabled={!selected.length} onClick={enqueue}><Upload size={15} />开始上传</button></div>
        </>}
        <ul className="transfer-list" aria-label="上传任务">
          {visible.map(task => <li key={task.key}>
            <div className="transfer-heading"><FileText size={17} /><strong title={task.name}>{task.name}</strong><span>{size(task.size)}</span></div>
            <div className="transfer-state" aria-live="polite">
              {task.state === "ready" ? <Check size={14} /> : ["uploading", "verifying", "processing"].includes(task.state) ? <Loader2 size={14} className="spin" /> : null}
              <span>{labels[task.state] || "等待恢复"}</span>
              {task.state === "uploading" && <span>{Math.floor(Math.min(task.transferred, task.size) / task.size * 100)}%</span>}
              <small>{task.library.name}</small>
            </div>
            {task.state === "uploading" && <progress max={task.size} value={Math.min(task.transferred, task.size)} aria-label={`${task.name}传输进度`} />}
            {task.error && <p className="form-error" role="alert">{task.error}</p>}
            <div className="transfer-actions">
              {["uploading", "queued"].includes(task.state) && <Tooltip content="暂停传输"><button className="icon-button" aria-label={`暂停${task.name}`} onClick={() => {
                patch(task.key, { state: "paused" }); running.current.get(task.key)?.abort();
              }}><Pause size={16} /></button></Tooltip>}
              {["paused", "failed"].includes(task.state) && (files.current.has(task.key)
                ? <button className="secondary" onClick={() => resume(task)}><Play size={14} />{task.state === "failed" ? "重试" : "继续"}</button>
                : <label className="secondary transfer-reselect"><Upload size={14} />重选原文件继续<input type="file" aria-label={`重新选择${task.name}`} onChange={e => { if (e.target.files?.[0]) resume(task, e.target.files[0]); e.target.value = ""; }} /></label>)}
              {["verifying", "processing"].includes(task.state) && task.error && <button className="secondary" onClick={() => void poll()}><RefreshCw size={14} />重试查询</button>}
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
