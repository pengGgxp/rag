"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ExternalLink, Loader2, Monitor, RefreshCw, Square, X } from "lucide-react";
import { request } from "./api";
import { json } from "./admin/admin-ui";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { formatCredits } from "./credit-format";
import "./cloud-preview.css";

export type Preview = { id: string; name: string; status: string; summary: string; terminal: boolean; url: string | null };
type Computer = { id: string; status: string; terminal: boolean; credits: string; credits_per_minute: string;
  seconds: number; idle_ends_at: string | null; max_ends_at: string | null };
type Detail = { preview: Preview; computer: Computer; conversation_id: string };

export function openPreview(id: string) {
  window.dispatchEvent(new CustomEvent("zhiku:preview", { detail: id }));
}

function time(value: string | null) {
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
}

export function PreviewSurface({ id, standalone = false, onClose }: { id: string; standalone?: boolean; onClose?: () => void }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [visible, setVisible] = useState(true);
  const [frameLoaded, setFrameLoaded] = useState(false);
  const authorize = useCallback(async () => {
    return request<{ url: string }>(`/previews/${id}/access`, json({}));
  }, [id]);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    update(); document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => {
    let disposed = false, authorized = false, timer: ReturnType<typeof setTimeout> | undefined;
    setUrl(""); setError("");
    async function refresh() {
      try {
        const next = await request<Detail>(`/previews/${id}`);
        if (disposed) return;
        setDetail(next);
        const active = next.preview.status === "ready" && !next.computer.terminal && !["stopping", "uncertain"].includes(next.computer.status);
        if (active && !document.hidden) {
          const lease = await request<{ computer: Computer }>(`/previews/${id}/heartbeat`, json({}));
          if (disposed) return;
          setDetail({ ...next, computer: lease.computer });
          if (!authorized) {
            const grant = await authorize();
            if (disposed) return;
            setFrameLoaded(false); setUrl(grant.url); authorized = true;
          }
        } else if (!active) { setUrl(""); }
        if (!disposed) setError("");
      } catch (cause) {
        authorized = false;
        if (!disposed) { setUrl(""); setError(cause instanceof Error ? cause.message : "预览暂不可用，请刷新重试"); }
      }
      if (!disposed) timer = setTimeout(refresh, 20000);
    }
    if (visible) void refresh();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
    // Opening/refreshing obtains a new one-time entry; ordinary heartbeats preserve the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, visible, revision]);
  async function stop() {
    if (!detail) return;
    setBusy(true); setError("");
    try {
      await request(`/conversations/${detail.conversation_id}/cloud-computer/end`, json({}));
      setUrl(""); setRevision(value => value + 1);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "停止失败，请重试"); }
    finally { setBusy(false); }
  }
  const computer = detail?.computer;
  const active = computer && !computer.terminal && !["stopping", "uncertain"].includes(computer.status) && detail?.preview.status === "ready";
  return <section className={`cloud-preview-surface ${standalone ? "cloud-preview-standalone" : ""}`} aria-label="成果预览">
    <header className="cloud-preview-heading">
      <Monitor size={17} aria-hidden="true" /><h2>{detail?.preview.name || "成果预览"}</h2>
      <div className="cloud-preview-actions">
        <Tooltip content="刷新预览"><button type="button" className="icon-button" aria-label="刷新预览" disabled={busy} onClick={() => { setUrl(""); setRevision(value => value + 1); }}><RefreshCw size={16} /></button></Tooltip>
        {!standalone && <Tooltip content="新窗口打开"><a className="icon-button" aria-label="新窗口打开预览" href={`/previews/${id}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} /></a></Tooltip>}
        {onClose && <Tooltip content="关闭预览"><button type="button" className="icon-button" aria-label="关闭预览" onClick={onClose}><X size={17} /></button></Tooltip>}
      </div>
    </header>
    <div className="cloud-preview-meter" aria-live="polite">
      {computer && <>
        <span className={active ? "cloud-preview-live" : ""}>{active ? "运行中" : computer.terminal ? "已停止" : detail?.preview.summary}</span>
        <span>{Math.floor(computer.seconds / 60)} 分 {computer.seconds % 60} 秒</span>
        <span>{formatCredits(Number(computer.credits_per_minute))} 积分/分钟</span>
        <strong>{formatCredits(Number(computer.credits))} 积分</strong>
        {active && <span>最晚 {time(computer.max_ends_at)} 结束</span>}
      </>}
      {computer && !computer.terminal && <button type="button" className="secondary cloud-preview-stop" disabled={busy || computer.status === "stopping"} onClick={() => void stop()}><Square size={13} />停止云电脑</button>}
    </div>
    {active && <p className="cloud-preview-notice">预览可见期间持续计费；关闭后按闲置规则结束{computer?.idle_ends_at ? `，当前预计 ${time(computer.idle_ends_at)}` : ""}。</p>}
    {error && <div className="cloud-preview-error" role="alert">{error}</div>}
    <div className="cloud-preview-canvas">
      {url && active ? <>
        {!frameLoaded && <div className="cloud-preview-loading" role="status"><Loader2 size={20} className="spin" />正在打开预览</div>}
        <iframe key={url} src={url} title={detail?.preview.name || "成果预览"} referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" onLoad={() => setFrameLoaded(true)} />
      </> : <div className="cloud-preview-empty" role="status"><Monitor size={28} /><p>{error ? "预览暂不可用" : detail?.preview.summary || "正在获取预览状态"}</p>
        {computer?.terminal && <a href="/resources">打开资源库</a>}
      </div>}
    </div>
  </section>;
}

export function ConversationPreview({ conversationId }: { conversationId: string | null }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const [narrow, setNarrow] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    setPreview(null); setOpened(null);
    if (!conversationId) return;
    let disposed = false;
    const load = async () => {
      if (document.hidden) return;
      try {
        const data = await request<{ preview: Preview | null }>(`/conversations/${conversationId}/preview`);
        if (!disposed) setPreview(data.preview);
      } catch { /* The cloud computer panel owns connection errors. */ }
    };
    void load();
    const timer = setInterval(load, 5000);
    const show = (event: Event) => { setOpened((event as CustomEvent<string>).detail); void load(); };
    const resize = () => setNarrow(window.innerWidth < 1000);
    resize(); window.addEventListener("resize", resize); window.addEventListener("zhiku:preview", show);
    return () => { disposed = true; clearInterval(timer); window.removeEventListener("resize", resize); window.removeEventListener("zhiku:preview", show); };
  }, [conversationId]);
  const close = useCallback(() => { setOpened(null); requestAnimationFrame(() => trigger.current?.focus()); }, []);
  useEffect(() => {
    if (!opened || narrow) return;
    document.body.classList.add("has-cloud-preview");
    document.querySelector<HTMLButtonElement>(".cloud-preview-dock button")?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", escape);
    return () => { document.body.classList.remove("has-cloud-preview"); window.removeEventListener("keydown", escape); };
  }, [opened, narrow, close]);
  return <>
    {preview && <button ref={trigger} type="button" className="text-button cloud-preview-open" onClick={() => setOpened(preview.id)}><Monitor size={14} />{preview.status === "ready" ? "打开预览" : "查看预览状态"}</button>}
    {opened && (narrow ? <Dialog title="成果预览" fullScreen onClose={close}><PreviewSurface id={opened} onClose={close} /></Dialog>
      : createPortal(<aside className="cloud-preview-dock"><PreviewSurface id={opened} onClose={close} /></aside>, document.body))}
  </>;
}
