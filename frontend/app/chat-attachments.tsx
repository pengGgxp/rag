"use client";

import { useEffect, useRef, useState } from "react";
import { Download, FileImage, FileText, Files, Loader2, Paperclip, RotateCw, X } from "lucide-react";
import { API_URL, downloadFile, request, sessionHeaders } from "./api";
import { Tooltip } from "../components/ui/tooltip";
import { Modal } from "./modal";
import { ResourceLibrary, type ResourceFile } from "./resource-library";
import { ResourceNotice } from "./resource-notice";
import { attachmentAccept } from "./attachment-formats";
import { useAuth } from "./auth";
import "./chat-attachments.css";

export type ChatAttachment = {
  id: string; filename: string; size_bytes: number; content_type: string; kind: "image" | "document";
  resource_id?: string; version?: number; available?: boolean;
};
type DraftAttachment = {
  key: string; file: File; attachment?: ChatAttachment; status: "uploading" | "ready" | "failed"; error?: string;
  existing?: boolean;
};
export { attachmentAccept } from "./attachment-formats";
const supportedFile = (file: File) => /\.(png|jpe?g|webp|pdf|txt|md|csv|docx|xlsx)$/i.test(file.name);
const imageFile = (file: File) => /\.(png|jpe?g|webp)$/i.test(file.name);
function size(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.ceil(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function useChatAttachments() {
  const { role } = useAuth();
  const [items, setItems] = useState<DraftAttachment[]>([]);
  const current = useRef(items);
  current.current = items;
  const controllers = useRef(new Map<string, AbortController>());
  const revision = useRef(0);
  function reset(discard = true) {
    revision.current++;
    controllers.current.forEach(controller => controller.abort());
    controllers.current.clear();
    current.current = [];
    setItems([]);
  }
  useEffect(() => () => {
    revision.current++;
    controllers.current.forEach(controller => controller.abort());
  }, []);
  async function upload(item: DraftAttachment) {
    const epoch = revision.current;
    const controller = new AbortController();
    controllers.current.set(item.key, controller);
    setItems(values => values.map(value => value.key === item.key ? { ...value, status: "uploading", error: undefined } : value));
    try {
      const form = new FormData();
      form.set("file", item.file);
      const attachment = await request<ChatAttachment>("/chat/attachments", { method: "POST", body: form, signal: controller.signal });
      if (epoch === revision.current && !controller.signal.aborted)
        setItems(values => values.map(value => value.key === item.key ? { ...value, attachment, status: "ready" } : value));
    } catch (error) {
      if (epoch === revision.current && !controller.signal.aborted)
        setItems(values => values.map(value => value.key === item.key ? { ...value, status: "failed",
          error: error instanceof Error ? error.message : "上传失败，请重试" } : value));
    } finally {
      if (controllers.current.get(item.key) === controller) controllers.current.delete(item.key);
    }
  }
  function add(files: File[], supportsImages: boolean) {
    if (role !== "admin" && role !== "editor") return "当前工作区为只读权限，可从资源库选择已有文件";
    if (files.some(file => !supportedFile(file))) return "请选择图片、PDF、TXT、Markdown、CSV、Word 或 Excel 文件";
    if (current.current.length + files.length > 6) return "每次最多添加 6 个附件";
    if (files.some(file => imageFile(file) && !supportsImages)) return "当前模型不支持图片，请先选择支持图片的模型";
    if (files.some(file => file.size > (imageFile(file) ? 10 : 20) * 1024 * 1024))
      return "图片不能超过 10 MiB，文件不能超过 20 MiB";
    if ([...current.current.map(item => item.file), ...files].reduce((total, file) => total + file.size, 0) > 40 * 1024 * 1024)
      return "每次附件总计不能超过 40 MiB";
    const next = files.map(file => ({ key: crypto.randomUUID(), file, status: "uploading" as const }));
    current.current = [...current.current, ...next];
    setItems(current.current);
    next.forEach(item => void upload(item));
    return "";
  }
  function remove(item: DraftAttachment) {
    controllers.current.get(item.key)?.abort();
    current.current = current.current.filter(value => value.key !== item.key);
    setItems(values => values.filter(value => value.key !== item.key));
  }
  async function selectResource(item: ResourceFile, supportsImages: boolean) {
    if (current.current.length >= 6) throw new Error("每次最多添加 6 个附件");
    if (item.content_type.startsWith("image/") && !supportsImages) throw new Error("当前模型不支持图片，请选择支持图片的模型");
    if (current.current.some(value => value.attachment?.resource_id === item.id))
      throw new Error("此资源已添加");
    if (current.current.reduce((total, value) => total + value.file.size, item.size_bytes) > 40 * 1024 * 1024)
      throw new Error("每次附件总计不能超过 40 MiB");
    const attachment = await request<ChatAttachment>(`/resources/${item.id}/attachment`, { method: "POST" });
    const file = new File([], attachment.filename, { type: attachment.content_type });
    Object.defineProperty(file, "size", { value: attachment.size_bytes });
    const next: DraftAttachment = { key: crypto.randomUUID(), file, attachment, status: "ready", existing: true };
    current.current = [...current.current, next]; setItems(current.current);
  }
  function restore(references: ChatAttachment[]) {
    reset(false);
    const restored = references.filter(item => item.resource_id && item.available !== false).slice(0, 6).map(attachment => {
      const file = new File([], attachment.filename, { type: attachment.content_type });
      Object.defineProperty(file, "size", { value: attachment.size_bytes });
      return { key: crypto.randomUUID(), file, attachment, status: "ready" as const, existing: true };
    });
    current.current = restored; setItems(restored);
  }
  return { items, add, selectResource, remove, retry: upload, reset, restore,
    ready: items.flatMap(item => item.attachment ? [item.attachment] : []),
    blocked: items.some(item => item.status !== "ready"),
    hasImages: items.some(item => imageFile(item.file)) };
}

function LocalThumbnail({ file }: { file: File }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const source = URL.createObjectURL(file);
    setUrl(source);
    return () => URL.revokeObjectURL(source);
  }, [file]);
  return <img src={url || undefined} alt="" />;
}

export function DraftAttachments({ items, disabled, onRemove, onRetry }: {
  items: DraftAttachment[]; disabled: boolean; onRemove: (item: DraftAttachment) => void; onRetry: (item: DraftAttachment) => void;
}) {
  if (!items.length) return null;
  return <><ResourceNotice /><ul className="chat-attachments" aria-label="待发送附件" aria-live="polite">{items.map(item => <li key={item.key}>
    <span className="chat-attachment-thumb">{imageFile(item.file) ? item.existing ? <FileImage size={22} /> : <LocalThumbnail file={item.file} /> : <FileText size={22} />}</span>
    <span className="chat-attachment-copy"><strong title={item.file.name}>{item.file.name}</strong>
      <small className={item.error ? "form-error" : ""}>{item.status === "uploading" ? "正在上传" : item.error || size(item.file.size)}</small></span>
    {item.status === "uploading" && <Loader2 size={15} className="spin" aria-label="正在上传" />}
    {item.status === "failed" && <Tooltip content="重试上传"><button type="button" disabled={disabled}
      aria-label={`重试上传 ${item.file.name}`} onClick={() => onRetry(item)}><RotateCw size={16} /></button></Tooltip>}
    <Tooltip content="移除附件"><button type="button" disabled={disabled} aria-label={`移除 ${item.file.name}`}
      onClick={() => onRemove(item)}><X size={16} /></button></Tooltip>
  </li>)}</ul></>;
}

function SentAttachment({ item }: { item: ChatAttachment }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (item.kind !== "image" || item.available === false) return;
    const controller = new AbortController();
    let source = "";
    setError("");
    fetch(`${API_URL}/chat/attachments/${item.id}/content`, { credentials: "include", headers: sessionHeaders(), signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error("图片加载失败");
        const blob = await response.blob();
        if (!controller.signal.aborted) { source = URL.createObjectURL(blob); setUrl(source); }
      }).catch(() => { if (!controller.signal.aborted) setError("图片加载失败"); });
    return () => { controller.abort(); if (source) URL.revokeObjectURL(source); };
  }, [item.id, item.kind, item.available, attempt]);
  async function download() {
    try { await downloadFile(`/chat/attachments/${item.id}/content`, item.filename); }
    catch (e) { setError(e instanceof Error ? e.message : "下载失败，请重试"); }
  }
  return <li>
    <button type="button" className="chat-attachment-open" disabled={item.available === false} onClick={() => {
      if (error && item.kind === "image") setAttempt(value => value + 1);
      else if (item.kind === "image" && url) setOpen(true);
      else if (item.kind !== "image") void download();
    }} aria-label={error ? `${error}，重试` : `${item.kind === "image" ? "查看" : "下载"} ${item.filename}`}>
      <span className="chat-attachment-thumb">{item.kind === "image" && url ? <img src={url} alt={item.filename} /> :
        item.kind === "image" && !error && item.available !== false ? <Loader2 size={18} className="spin" /> : <FileText size={22} />}</span>
      <span className="chat-attachment-copy"><strong>{item.filename}</strong><small>{item.available === false ? "资源已删除" : error || size(item.size_bytes)}</small></span>
      {item.kind !== "image" && <Download size={16} />}
    </button>
    {open && <Modal title={item.filename} onClose={() => setOpen(false)} wide>
      <img className="chat-image-preview" src={url} alt={item.filename} />
      <div className="modal-footer"><button className="secondary" onClick={() => void download()}><Download size={16} />下载图片</button></div>
      {error && <p className="form-error" role="alert">{error}</p>}
    </Modal>}
  </li>;
}

export function MessageAttachments({ items }: { items?: ChatAttachment[] }) {
  return items?.length ? <ul className="chat-attachments chat-sent-attachments" aria-label="消息附件">
    {items.map(item => <SentAttachment key={item.id} item={item} />)}
  </ul> : null;
}

export function AttachmentButton({ disabled, add, select }: { disabled: boolean; add: (files: File[]) => void;
  select: (item: ResourceFile) => Promise<void> }) {
  const { role } = useAuth();
  const canWrite = role === "admin" || role === "editor";
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [library, setLibrary] = useState(false);
  return <><input ref={input} type="file" className="chat-attachment-input" accept={attachmentAccept} multiple
    tabIndex={-1} disabled={disabled || !canWrite} onChange={event => { add(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
    <Tooltip content="添加图片或文件"><button type="button" className="chat-attach-button" disabled={disabled}
      aria-label="添加图片或文件" onClick={() => setOpen(true)}><Paperclip size={18} /></button></Tooltip>
    {open && <Modal title={library ? "选择资源" : "添加附件"} wide={library} restoreFocusSelector=".chat-attach-button" onClose={() => { setOpen(false); setLibrary(false); }}>
      {library ? <ResourceLibrary select={async item => { await select(item); setOpen(false); setLibrary(false); }} /> :
        <div className="attachment-source-body"><div className="attachment-source-actions">{canWrite && <button type="button" onClick={() => { setOpen(false); input.current?.click(); }}><span className="attachment-source-icon"><Paperclip size={20} /></span><span><strong>上传文件</strong><small>从设备选择图片或文档</small></span></button>}
          <button type="button" onClick={() => setLibrary(true)}><span className="attachment-source-icon"><Files size={20} /></span><span><strong>从资源库选择</strong><small>使用工作区已保存的文件</small></span></button></div><ResourceNotice /></div>}
    </Modal>}
  </>;
}
