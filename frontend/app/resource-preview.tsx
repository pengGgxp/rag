"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Download, File, FileText, ImageIcon, Info, Loader2, Maximize, Minus, Plus, RotateCcw } from "lucide-react";
import { downloadFile } from "./api";
import { Modal } from "./modal";
import { Tooltip } from "../components/ui/tooltip";
import { FileReaderSession, fileDownload, type FileReference } from "./file-preview-access";
import { pdfPreviewError } from "./pdf-preview-error";
import type { ResourceFile } from "./resource-library";
import type { PDFDocumentProxy, PDFDocumentLoadingTask } from "pdfjs-dist";
import type { OfficePages } from "./office-preview";
import "./resource-preview.css";

function useFileSession(file: FileReference, scope: string, attempt: number, enabled = true) {
  const identity = `${scope}:${file.type}:${file.id}:${attempt}`;
  const [state, setState] = useState<{ identity: string; reader: FileReaderSession | null; error: string }>({ identity, reader: null, error: "" });
  useEffect(() => {
    setState({ identity, reader: null, error: "" });
    if (!enabled) return;
    const controller = new AbortController();
    const session = new FileReaderSession(file, scope, controller.signal);
    let checking = false;
    const validate = async () => {
      if (checking || controller.signal.aborted) return;
      checking = true;
      try { await session.validate(); if (!controller.signal.aborted) setState({ identity, reader: session, error: "" }); }
      catch (cause) {
        if (!controller.signal.aborted) { setState({ identity, reader: null, error: cause instanceof Error ? cause.message : "文件未能加载，请重试" }); controller.abort(); }
      } finally { checking = false; }
    };
    const foreground = () => { if (!document.hidden) void validate(); };
    void validate();
    const interval = setInterval(foreground, 30_000);
    document.addEventListener("visibilitychange", foreground);
    return () => { controller.abort(); clearInterval(interval); document.removeEventListener("visibilitychange", foreground); };
  }, [identity, enabled]);
  return state.identity === identity && enabled ? state : { reader: null, error: "" };
}

function Tool({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return <Tooltip content={label}><button className="icon-button" type="button" aria-label={label} disabled={disabled} onClick={onClick}>{children}</button></Tooltip>;
}

type InlineImageProps = { file: FileReference; scope: string; onOpen: (element: HTMLElement) => void };
export function InlineFileImage(props: InlineImageProps) {
  return <InlineImage key={`${props.scope}:${props.file.type}:${props.file.id}`} {...props} />;
}

function InlineImage({ file, scope, onOpen }: InlineImageProps) {
  const element = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false), [attempt, setAttempt] = useState(0), [url, setUrl] = useState(""), [error, setError] = useState("");
  const { reader, error: accessError } = useFileSession(file, scope, attempt, visible);
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: "160px" });
    if (element.current) observer.observe(element.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    setUrl(""); setError("");
    if (!reader) return;
    let disposed = false, source = "";
    void reader.read().then(blob => { if (!disposed) { source = URL.createObjectURL(blob); setUrl(source); } })
      .catch(() => { if (!disposed && !reader.signal.aborted) setError("图片未能加载，请重试"); });
    return () => { disposed = true; if (source) URL.revokeObjectURL(source); };
  }, [reader]);
  return <div ref={element} className="inline-file-image">
    {accessError || error ? <div className="inline-image-failed"><ImageIcon size={24} /><span>{accessError || error}</span><button type="button" className="secondary" onClick={() => setAttempt(value => value + 1)}>重试</button></div>
      : url ? <button type="button" className="inline-image-open" aria-label={`放大图片 ${file.name}`} onClick={event => onOpen(event.currentTarget)}><img src={url} alt={file.name} onError={() => setError("图片未能加载，请重试")} /></button>
        : <div className="inline-image-loading" role="status"><Loader2 className="spin" size={20} /><span>正在加载图片</span></div>}
  </div>;
}

let active = 0;
const waiters: (() => void)[] = [];
async function limited<T>(action: () => Promise<T>): Promise<T> {
  if (active >= 3) await new Promise<void>(resolve => waiters.push(resolve));
  active++;
  try { return await action(); } finally { active--; waiters.shift()?.(); }
}

type ThumbnailProps = { item: ResourceFile; scope: string; compact?: boolean };
export function ResourceThumbnail(props: ThumbnailProps) {
  return <Thumbnail key={`${props.scope}:${props.item.id}`} {...props} />;
}

function Thumbnail({ item, scope, compact = false }: ThumbnailProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false), [url, setUrl] = useState(""), [ratio, setRatio] = useState(1.2);
  const image = item.content_type.startsWith("image/") && !/svg/i.test(item.content_type);
  const pdf = /\.pdf$/i.test(item.path), documentFile = /\.(pdf|docx?|pptx?)$/i.test(item.path);
  const { reader } = useFileSession({ type: "resource", id: item.id, name: item.path }, scope, 0, visible && (image || pdf) && item.availability !== "missing");
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: "160px" });
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    setUrl(""); if (!reader) return;
    let disposed = false, source = "";
    void limited(async () => {
      if (disposed) return;
      let blob: Blob;
      if (image) blob = await reader.read();
      else {
        const { loadPdf } = await import("./resource-pdf");
        const loading = await loadPdf(reader);
        try {
          const doc = await loading.promise, page = await doc.getPage(1);
          const viewport = page.getViewport({ scale: 240 / page.getViewport({ scale: 1 }).width });
          const canvas = window.document.createElement("canvas"); canvas.width = viewport.width; canvas.height = viewport.height;
          await page.render({ canvas, viewport }).promise;
          blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(), "image/webp"));
        } finally { await loading.destroy(); }
      }
      if (!disposed) { source = URL.createObjectURL(blob); setUrl(source); }
    }).catch(() => {});
    return () => { disposed = true; if (source) URL.revokeObjectURL(source); };
  }, [reader, image]);
  return <div ref={ref} className={`resource-cover ${image ? "resource-cover-image" : documentFile ? "resource-cover-document" : "resource-cover-other"} ${compact ? "resource-cover-compact" : ""}`}
    style={!compact ? { aspectRatio: image ? Math.max(.55, Math.min(1.8, ratio)) : .77 } : undefined}>
    {url ? <img src={url} alt="" onLoad={event => setRatio(event.currentTarget.naturalWidth / event.currentTarget.naturalHeight)} /> : <div className="resource-cover-placeholder">
      {image ? <ImageIcon size={compact ? 22 : 40} strokeWidth={1.2} /> : documentFile ? <FileText size={compact ? 22 : 40} strokeWidth={1.2} /> : <File size={compact ? 22 : 40} strokeWidth={1.2} />}
      {!compact && <span>{item.path.split(".").pop()?.toUpperCase()}</span>}
    </div>}
  </div>;
}

type PageProps = { reader: FileReaderSession; page: number; zoom: number; onCount: (count: number) => void; onError: (error: string) => void };

function PdfPage({ reader, page, zoom, onCount, onError }: PageProps) {
  const host = useRef<HTMLDivElement>(null), canvas = useRef<HTMLCanvasElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null), [width, setWidth] = useState(700), [busy, setBusy] = useState(true);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(100, entry.contentRect.width - 48)));
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let disposed = false, loading: PDFDocumentLoadingTask | undefined;
    void import("./resource-pdf").then(module => module.loadPdf(reader, () => {
      if (!disposed) onError("页面未能加载，请重试或下载查看");
    })).then(task => {
      loading = task; if (disposed) { void task.destroy(); return; }
      return task.promise.then(value => {
        if (disposed) return;
        if (value.numPages === 0) { onError("PDF 文件为空或没有页面，请重新导出后上传"); return; }
        setPdf(value); onCount(value.numPages);
      });
    }).catch(cause => { if (!disposed && !reader.signal.aborted) onError(pdfPreviewError(cause)); });
    return () => { disposed = true; void loading?.destroy(); };
  }, [reader, onCount, onError]);
  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let disposed = false, task: ReturnType<Awaited<ReturnType<PDFDocumentProxy["getPage"]>>["render"]> | undefined;
    setBusy(true);
    void pdf.getPage(page).then(value => {
      if (disposed || !canvas.current) return;
      const scale = width / value.getViewport({ scale: 1 }).width * zoom;
      const viewport = value.getViewport({ scale }), ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.current.width = Math.ceil(viewport.width * ratio); canvas.current.height = Math.ceil(viewport.height * ratio);
      canvas.current.style.width = `${viewport.width}px`; canvas.current.style.height = `${viewport.height}px`;
      task = value.render({ canvas: canvas.current, viewport: value.getViewport({ scale: scale * ratio }) });
      return task.promise;
    }).then(() => { if (!disposed) setBusy(false); }).catch(cause => {
      if (!disposed && cause.name !== "RenderingCancelledException") onError("页面未能加载，请重试");
    });
    return () => { disposed = true; task?.cancel(); };
  }, [pdf, page, width, zoom, onError]);
  return <div ref={host} className="file-page-stage">{busy && <div className="file-page-loading" role="status"><Loader2 size={18} className="spin" />正在加载页面</div>}<canvas ref={canvas} aria-label={`第 ${page} 页`} /></div>;
}

function OfficePage({ reader, page, zoom, onCount, onError }: PageProps) {
  const [doc, setDoc] = useState<OfficePages | null>(null), [width, setWidth] = useState(700);
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(100, entry.contentRect.width - 32)));
    if (host.current) observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let disposed = false, result: OfficePages | undefined;
    void reader.read().then(async blob => (await import("./office-preview")).renderOffice(blob, reader.access.kind as "docx" | "pptx", reader.signal))
      .then(value => { result = value; if (disposed) value.dispose(); else { setDoc(value); onCount(value.pages.length); } })
      .catch(cause => {
        if (disposed || reader.signal.aborted) return;
        const known = ["文件较大，请下载后查看", "文档内容较大，请下载后查看", "文档已加密或格式不受支持，请下载后查看",
          "文档已加密，请解密后重试或下载查看", "文档无法安全读取，请下载后查看", "文档内容损坏，请重新导出"];
        onError(cause instanceof Error && known.includes(cause.message) ? cause.message : "文档未能打开，可能已加密或损坏，请下载查看");
      });
    return () => { disposed = true; result?.dispose(); };
  }, [reader, onCount, onError]);
  const scale = doc ? Math.min(1, width / doc.width) * zoom : 1;
  return <div ref={host} className="file-page-stage">{doc ? <div className="office-page-size" style={{ width: doc.width * scale, height: doc.height * scale }}>
    <iframe title={`第 ${page} 页`} sandbox="" referrerPolicy="no-referrer" srcDoc={doc.pages[page - 1]} style={{ width: doc.width, height: doc.height, transform: `scale(${scale})`, transformOrigin: "top left" }} />
  </div> : <div className="resource-reader-empty" role="status"><Loader2 size={22} className="spin" />正在打开文档</div>}</div>;
}

type ViewerProps = { file: FileReference; scope: string; onClose: () => void; restoreFocusSelector?: string; alternate?: ReactNode; initialPage?: number };
export function FilePreview(props: ViewerProps) {
  return <FileViewer key={`${props.scope}:${props.file.type}:${props.file.id}`} {...props} />;
}

function FileViewer({ file, scope, onClose, restoreFocusSelector, alternate, initialPage = 1 }: ViewerProps) {
  const [attempt, setAttempt] = useState(0), [error, setError] = useState(""), [url, setUrl] = useState(""), [text, setText] = useState<string | null>(null), [truncated, setTruncated] = useState(false);
  const [page, setPage] = useState(initialPage), [count, setCount] = useState(0), [zoom, setZoom] = useState(1), [details, setDetails] = useState(false), [downloadError, setDownloadError] = useState("");
  const { reader, error: accessError } = useFileSession(file, scope, attempt);
  const [drag, setDrag] = useState({ x: 0, y: 0 });
  const pointer = useRef<{ x: number; y: number; baseX: number; baseY: number } | null>(null);
  useEffect(() => {
    setUrl(""); setText(null); setError(""); setPage(initialPage); setCount(0); setZoom(1); setDrag({ x: 0, y: 0 });
    if (!reader || !["image", "text"].includes(reader.access.kind)) return;
    let disposed = false, source = "";
    const textLimit = 4 * 1024 * 1024;
    const partial = reader.access.kind === "text" && reader.access.size_bytes > textLimit;
    void reader.read(partial ? [0, textLimit] : undefined).then(async blob => {
      if (disposed) return;
      if (reader.access.kind === "text") { const value = await blob.text(); if (!disposed) { setText(value.slice(0, 1_000_000)); setTruncated(partial || value.length > 1_000_000); } }
      else { source = URL.createObjectURL(blob); setUrl(source); }
    }).catch(() => { if (!disposed && !reader.signal.aborted) setError("文件未能加载，请重试或下载查看"); });
    return () => { disposed = true; if (source) URL.revokeObjectURL(source); };
  }, [reader, initialPage]);
  const setPageCount = useCallback((total: number) => {
    setCount(total);
    if (total > 0) setPage(value => Math.min(value, total));
  }, []);
  const kind = reader?.access.kind, paged = kind === "pdf" || kind === "docx" || kind === "pptx";
  const failure = accessError || error;
  const download = <Tooltip content="下载原文件"><button type="button" className="file-download icon-button" aria-label="下载原文件" onClick={() => void downloadFile(fileDownload(file), file.name).catch(() => setDownloadError("下载失败，请重试"))}><Download size={18} /><span>下载原文件</span></button></Tooltip>;
  const tools = <div className="file-viewer-tools">
    {paged && <><Tool label="上一页" disabled={page <= 1 || !count} onClick={() => setPage(value => value - 1)}><ChevronLeft size={18} /></Tool><span className="file-page-count">{page} / {count || "—"}</span><Tool label="下一页" disabled={page >= count} onClick={() => setPage(value => value + 1)}><ChevronRight size={18} /></Tool></>}
    {(paged || kind === "image") && <><Tool label="缩小" disabled={zoom <= .25} onClick={() => setZoom(value => Math.max(.25, value - .25))}><Minus size={18} /></Tool><span className="file-zoom">{Math.round(zoom * 100)}%</span><Tool label="放大" disabled={zoom >= 4} onClick={() => setZoom(value => Math.min(4, value + .25))}><Plus size={18} /></Tool><Tool label="适应窗口" onClick={() => { setZoom(1); setDrag({ x: 0, y: 0 }); }}><Maximize size={18} /></Tool></>}
  </div>;
  return <Modal title={file.name.split("/").pop() || file.name} wide onClose={onClose} restoreFocusSelector={restoreFocusSelector}
    initialFocusSelector='.modal-heading button[aria-label="关闭"]'
    headerActions={<div className="file-viewer-header-actions">{tools}<Tool label="文件信息" onClick={() => setDetails(value => !value)}><Info size={18} /></Tool>{download}</div>}>
    <div className={`file-viewer ${kind === "unsupported" ? "file-viewer-unsupported" : ""}`}>
      {details && <div className="file-viewer-info">{reader ? `${reader.access.filename} · ${reader.access.content_type} · ${(reader.access.size_bytes / 1024).toFixed(1)} KB` : file.name}</div>}
      {alternate}
      {failure ? <div className="resource-reader-empty" role="alert"><FileText size={36} strokeWidth={1.2} /><p>{failure}</p><button className="secondary" type="button" onClick={() => { setError(""); setAttempt(value => value + 1); }}><RotateCcw size={16} />重试</button></div>
        : reader && kind === "pdf" ? <PdfPage reader={reader} page={page} zoom={zoom} onCount={setPageCount} onError={setError} />
          : reader && (kind === "docx" || kind === "pptx") ? <OfficePage reader={reader} page={page} zoom={zoom} onCount={setPageCount} onError={setError} />
            : reader && url ? <div className="resource-image-stage" onPointerDown={event => { if (zoom <= 1) return; event.currentTarget.setPointerCapture(event.pointerId); pointer.current = { x: event.clientX, y: event.clientY, baseX: drag.x, baseY: drag.y }; }}
              onPointerMove={event => { if (pointer.current) setDrag({ x: pointer.current.baseX + event.clientX - pointer.current.x, y: pointer.current.baseY + event.clientY - pointer.current.y }); }} onPointerUp={() => { pointer.current = null; }} onPointerCancel={() => { pointer.current = null; }}>
              <img src={url} alt={file.name} draggable={false} onError={() => setError("图片未能加载，请重试")} style={{ transform: `translate(${drag.x}px, ${drag.y}px) scale(${zoom})` }} /></div>
              : reader && text !== null ? <>{truncated && <div className="file-viewer-info">仅显示部分文字，完整内容请下载查看</div>}<pre className="resource-plain-text">{text}</pre></>
                : kind === "unsupported" ? <div className="resource-reader-empty"><File size={36} strokeWidth={1.2} /><p>此格式请下载后查看</p></div>
                  : <div className="resource-reader-empty" role="status"><Loader2 size={24} className="spin" />正在打开文件</div>}
      {downloadError && <p className="file-download-error" role="alert">{downloadError}</p>}
    </div>
  </Modal>;
}

export function ResourcePreview({ item, ...props }: { item: ResourceFile; scope: string; onClose: () => void; restoreFocusSelector?: string }) {
  return <FilePreview file={{ type: "resource", id: item.id, name: item.path }} {...props} />;
}
