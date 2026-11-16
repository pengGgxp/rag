"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, File, FileText, ImageIcon, Loader2, Maximize, Minus, Plus, RotateCcw } from "lucide-react";
import { request, downloadFile } from "./api";
import { Modal } from "./modal";
import { Tooltip } from "../components/ui/tooltip";
import { cachedPreview } from "./resource-preview-cache";
import type { ResourceFile } from "./resource-library";
import type { PDFDocumentProxy, PDFDocumentLoadingTask } from "pdfjs-dist";
import "./resource-preview.css";

type Artifact = { id: string; kind: string; status: string; error: string | null; url: string | null; size_bytes: number; width: number | null; height: number | null };
export type PreviewInfo = { revision: string; kind: string; size_bytes: number; original_url: string; previews: Artifact[] };
const jobs = new Map<string, Promise<PreviewInfo>>();

export function inspectPreview(id: string): Promise<PreviewInfo> {
  if (jobs.has(id)) return jobs.get(id)!;
  const job = request<PreviewInfo>(`/resources/${id}/preview`).finally(() => jobs.delete(id));
  jobs.set(id, job); return job;
}

let thumbnailActive = 0;
const thumbnailWaiters: (() => void)[] = [];
async function limited<T>(operation: () => Promise<T>): Promise<T> {
  if (thumbnailActive >= 3) await new Promise<void>(resolve => thumbnailWaiters.push(resolve));
  thumbnailActive++;
  try { return await operation(); }
  finally { thumbnailActive--; thumbnailWaiters.shift()?.(); }
}

export function ResourceThumbnail({ item, scope, compact = false }: { item: ResourceFile; scope: string; compact?: boolean }) {
  const element = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState("");
  const [ratio, setRatio] = useState(item.content_type.startsWith("image/") ? 1.2 : 0.77);
  const [state, setState] = useState("");
  const [generation, setGeneration] = useState(0);
  const extension = item.path.split(".").pop()?.toUpperCase() || "FILE";
  const image = item.content_type.startsWith("image/");
  const document = /pdf|word|officedocument/.test(item.content_type) || /\.(docx?|pdf)$/i.test(item.path);
  useEffect(() => {
    const refresh = (event: Event) => {
      if ((event as CustomEvent<string>).detail === item.id) setGeneration(value => value + 1);
    };
    window.addEventListener("resource-preview-ready", refresh);
    return () => window.removeEventListener("resource-preview-ready", refresh);
  }, [item.id]);
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) setVisible(true); }, { rootMargin: "160px" });
    if (element.current) observer.observe(element.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible || item.availability === "missing" || (!image && !document)) return;
    let disposed = false, objectUrl = "", timer: ReturnType<typeof setTimeout> | undefined;
    setUrl("");
    async function load() {
      await limited(async () => {
        if (disposed) return;
        try {
          const info = await inspectPreview(item.id);
          let cover = info.previews.find(value => value.kind === "thumbnail");
          if ((!cover || cover.status === "expired") && info.kind !== "word") cover = await request<Artifact>(`/resources/${item.id}/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "thumbnail", retry: true }) });
          if (disposed) return;
          setState(cover?.status || "");
          if (cover?.status === "ready" && cover.url) {
            const blob = await cachedPreview(scope, info.revision, cover.url);
            if (disposed) return;
            objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
            if (cover.width && cover.height) setRatio(cover.width / cover.height);
          } else if (cover && ["queued", "running"].includes(cover.status)) timer = setTimeout(() => void load(), 2000);
        } catch { if (!disposed) setState("failed"); }
      });
    }
    void load();
    return () => { disposed = true; clearTimeout(timer); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [visible, item.id, item.updated_at, scope, image, document, item.availability, generation]);
  return <div ref={element} className={`resource-cover ${image ? "resource-cover-image" : document ? "resource-cover-document" : "resource-cover-other"} ${compact ? "resource-cover-compact" : ""}`}
    style={!compact ? { aspectRatio: Math.max(0.55, Math.min(1.8, ratio)) } : undefined}>
    {url ? <img src={url} alt="" loading="lazy" /> : <div className="resource-cover-placeholder">
      {image ? <ImageIcon size={compact ? 22 : 40} strokeWidth={1.2} /> : document ? <FileText size={compact ? 22 : 44} strokeWidth={1.15} /> : <File size={compact ? 22 : 40} strokeWidth={1.2} />}
      {!compact && <><span>{extension}</span>{["queued", "running"].includes(state) && <Loader2 size={14} className="spin" />}</>}
    </div>}
  </div>;
}

function Tool({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: React.ReactNode }) {
  return <Tooltip content={label}><button className="icon-button" type="button" aria-label={label} disabled={disabled} onClick={onClick}>{children}</button></Tooltip>;
}

function PdfReader({ path, size, revision, scope }: { path: string; size: number; revision: string; scope: string }) {
  const container = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1), [zoom, setZoom] = useState(1), [width, setWidth] = useState(700);
  const [error, setError] = useState(""), [rendering, setRendering] = useState(true);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let disposed = false, loading: PDFDocumentLoadingTask | undefined;
    setDocument(null); setPage(1); setError("");
    void (async () => {
      try {
        const pdf = await import("pdfjs-dist");
        pdf.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const first = new Uint8Array(await (await cachedPreview(scope, revision, path, [0, Math.min(size, 65536)])).arrayBuffer());
        if (disposed) return;
        class Transport extends pdf.PDFDataRangeTransport {
          requestDataRange(begin: number, end: number) {
            void cachedPreview(scope, revision, path, [begin, Math.min(end, size)]).then(blob => blob.arrayBuffer()).then(data => {
              if (!disposed) this.onDataRange(begin, new Uint8Array(data));
            }).catch(cause => { if (!disposed) { setError(cause.message); void loading?.destroy(); } });
          }
        }
        loading = pdf.getDocument({ range: new Transport(size, first, true), length: size,
          rangeChunkSize: 65536, disableAutoFetch: true, disableStream: true, isEvalSupported: false });
        const value = await loading.promise;
        if (disposed) { await value.destroy(); return; }
        setDocument(value);
      } catch (cause) { if (!disposed) setError(cause instanceof Error ? cause.message : "文档无法打开，请下载查看"); }
    })();
    return () => { disposed = true; void loading?.destroy(); };
  }, [path, revision, scope, size]);
  useEffect(() => {
    if (!document || !canvas.current) return;
    let disposed = false, task: ReturnType<Awaited<ReturnType<PDFDocumentProxy["getPage"]>>["render"]> | undefined;
    setRendering(true);
    void document.getPage(page).then(value => {
      if (disposed || !canvas.current) return;
      const viewport = value.getViewport({ scale: 1 });
      const scale = Math.max(0.2, width / viewport.width) * zoom;
      const display = value.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.current.width = Math.ceil(display.width * ratio); canvas.current.height = Math.ceil(display.height * ratio);
      canvas.current.style.width = `${display.width}px`; canvas.current.style.height = `${display.height}px`;
      task = value.render({ canvas: canvas.current, viewport: value.getViewport({ scale: scale * ratio }) });
      return task.promise;
    }).then(() => { if (!disposed) setRendering(false); }).catch(cause => { if (!disposed && cause.name !== "RenderingCancelledException") setError("页面加载失败，请重新打开或下载"); });
    return () => { disposed = true; task?.cancel(); };
  }, [document, page, zoom, width]);
  return <div className="resource-pdf-reader">
    <div className="resource-reader-tools">
      <Tool label="上一页" disabled={!document || page <= 1} onClick={() => setPage(value => value - 1)}><ChevronLeft size={17} /></Tool>
      <span className="resource-page-count">{page} / {document?.numPages ?? "—"}</span>
      <Tool label="下一页" disabled={!document || page >= document.numPages} onClick={() => setPage(value => value + 1)}><ChevronRight size={17} /></Tool>
      <i /><Tool label="缩小" onClick={() => setZoom(value => Math.max(0.5, value - 0.25))}><Minus size={17} /></Tool>
      <span>{Math.round(zoom * 100)}%</span><Tool label="放大" onClick={() => setZoom(value => Math.min(3, value + 0.25))}><Plus size={17} /></Tool>
      <Tool label="适应宽度" onClick={() => setZoom(1)}><Maximize size={17} /></Tool>
    </div>
    <div className="resource-pdf-pages" ref={container}>
      {error ? <p role="alert" className="resource-reader-error">{error}</p> : <>{(!document || rendering) && <div className="resource-page-loading" role="status"><Loader2 size={21} className="spin" />正在加载页面</div>}<canvas ref={canvas} aria-label={`第 ${page} 页`} /></>}
    </div>
  </div>;
}

export function ResourcePreview({ item, scope, onClose, restoreFocusSelector }: {
  item: ResourceFile; scope: string; onClose: () => void; restoreFocusSelector?: string;
}) {
  const [info, setInfo] = useState<PreviewInfo | null>(null), [error, setError] = useState(""), [url, setUrl] = useState(""), [text, setText] = useState<string | null>(null);
  const [retry, setRetry] = useState(0), [zoom, setZoom] = useState(1);
  const [drag, setDrag] = useState({ x: 0, y: 0 });
  const pointer = useRef<{ x: number; y: number; baseX: number; baseY: number } | null>(null);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  useEffect(() => {
    let disposed = false, objectUrl = "", timer: ReturnType<typeof setTimeout> | undefined;
    let loading = false, announced = false;
    setError(""); setUrl(""); setText(null); setInfo(null); setArtifact(null);
    async function load(first = false) {
      if (loading || disposed) return;
      loading = true;
      try {
        const next = await inspectPreview(item.id);
        if (disposed) return;
        setInfo(next);
        if (next.kind === "word") {
          let document = next.previews.find(value => value.kind === "document");
          if (!document || document.status === "expired" || (first && retry && document.status === "failed")) {
            document = await request<Artifact>(`/resources/${item.id}/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "document", retry: true }) });
          }
          if (disposed) return;
          if (document.status === "failed") throw new Error(document.error || "预览准备失败，请重试或下载");
          setArtifact(document);
          if (document.status === "ready" && !announced) {
            announced = true;
            window.dispatchEvent(new CustomEvent("resource-preview-ready", { detail: item.id }));
          }
          if (document.status !== "ready") timer = setTimeout(() => void load(), 2000);
        } else if (next.kind === "image" || next.kind === "text") {
          if (next.kind === "image" && !objectUrl) {
            const cover = next.previews.find(value => value.kind === "thumbnail" && value.status === "ready");
            if (cover?.url) {
              const thumbnail = await cachedPreview(scope, next.revision, cover.url);
              if (disposed) return;
              objectUrl = URL.createObjectURL(thumbnail); setUrl(objectUrl);
            }
          }
          const blob = await cachedPreview(scope, next.revision, next.original_url);
          if (disposed) return;
          if (next.kind === "text") {
            const value = await blob.text();
            if (!disposed) setText(value.slice(0, 1000000));
          } else {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
          }
        }
      } catch (cause) { if (!disposed) setError(cause instanceof Error ? cause.message : "预览加载失败"); }
      finally { loading = false; }
    }
    const foreground = () => { if (!document.hidden) { clearTimeout(timer); void load(); } };
    document.addEventListener("visibilitychange", foreground);
    const validation = setInterval(() => { if (!document.hidden) void load(); }, 30000);
    void load(true);
    return () => { disposed = true; clearTimeout(timer); clearInterval(validation); document.removeEventListener("visibilitychange", foreground); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [item.id, item.updated_at, scope, retry]);
  const pdfPath = info?.kind === "pdf" ? info.original_url : artifact?.status === "ready" ? artifact.url : null;
  return <Modal title={item.path.split("/").pop()!} wide restoreFocusSelector={restoreFocusSelector ?? `[data-resource-id="${item.id}"]`} onClose={onClose}>
    <div className="resource-reader" aria-label="文件预览">
      <div className="resource-reader-topbar"><span>{item.path.split(".").pop()?.toUpperCase()}<span className="resource-reader-filename"> · {item.path}</span></span>
        <button type="button" className="secondary" onClick={() => void downloadFile(`/resources/${item.id}/download`, item.path.split("/").pop()!).catch(cause => setError(cause.message))}><Download size={15} />下载原文件</button></div>
      {error ? <div className="resource-reader-empty" role="alert"><FileText size={36} strokeWidth={1.2} /><p>{error}</p><button type="button" className="secondary" onClick={() => setRetry(value => value + 1)}><RotateCcw size={15} />重试</button></div>
        : pdfPath && info ? <PdfReader path={pdfPath} size={info.kind === "pdf" ? info.size_bytes : artifact!.size_bytes} revision={info.revision} scope={scope} />
        : url ? <><div className="resource-reader-tools"><Tool label="缩小" onClick={() => setZoom(value => Math.max(0.25, value - 0.25))}><Minus size={17} /></Tool><span>{Math.round(zoom * 100)}%</span><Tool label="放大" onClick={() => setZoom(value => Math.min(4, value + 0.25))}><Plus size={17} /></Tool><Tool label="适应窗口" onClick={() => { setZoom(1); setDrag({ x: 0, y: 0 }); }}><Maximize size={17} /></Tool></div>
          <div className="resource-image-stage" onPointerDown={event => { if (zoom <= 1) return; event.currentTarget.setPointerCapture(event.pointerId); pointer.current = { x: event.clientX, y: event.clientY, baseX: drag.x, baseY: drag.y }; }}
            onPointerMove={event => { if (pointer.current) setDrag({ x: pointer.current.baseX + event.clientX - pointer.current.x, y: pointer.current.baseY + event.clientY - pointer.current.y }); }} onPointerUp={() => { pointer.current = null; }} onPointerCancel={() => { pointer.current = null; }}>
            <img src={url} alt={item.path} draggable={false} style={{ transform: `translate(${drag.x}px,${drag.y}px) scale(${zoom})` }} /></div></>
        : text !== null ? <pre className="resource-plain-text">{text}</pre>
        : info?.kind === "unsupported" ? <div className="resource-reader-empty"><File size={38} strokeWidth={1.2} /><p>该格式暂不支持预览</p></div>
        : <div className="resource-reader-empty" role="status"><Loader2 size={26} className="spin" /><p>{info?.kind === "word" ? "正在准备文档预览" : "正在打开文件"}</p></div>}
    </div>
  </Modal>;
}
