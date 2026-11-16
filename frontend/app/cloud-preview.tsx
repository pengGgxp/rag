"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ExternalLink, FolderOpen, Loader2, Monitor, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { request } from "./api";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { useCloudComputer } from "./cloud-computer-context";
import { previewReady } from "./cloud-computer-state";
import { CloudStatusError, EndCloudComputer } from "./cloud-computer";
import "./cloud-preview.css";

export type { Preview } from "./cloud-computer-state";

export function openPreview(id: string, trigger?: HTMLElement) {
  window.dispatchEvent(new CustomEvent("zhiku:preview", { detail: { id, trigger } }));
}

export function PreviewSurface({ id, standalone = false, onClose }: {
  id: string; standalone?: boolean; onClose?: () => void;
}) {
  const { store, state } = useCloudComputer();
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [visible, setVisible] = useState(true);
  const [frameLoaded, setFrameLoaded] = useState(false);
  const detail = state.details[id];
  const active = previewReady(state, id);
  const computer = detail && state.computers[detail.computer.id];
  const sameComputer = !!computer;
  useEffect(() => {
    store.selectPreview(id);
    return () => store.selectPreview(null);
  }, [store, id]);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    update(); document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => {
    if (!active || !visible) return;
    void store.renew(id);
    const timer = setInterval(() => { if (!document.hidden) void store.renew(id); }, 20000);
    return () => clearInterval(timer);
  }, [store, id, active, visible]);
  useEffect(() => {
    if (!active) { setUrl(""); setError(""); return; }
    if (!visible) return;
    let disposed = false;
    const controller = new AbortController();
    setError("");
    void request<{ url: string }>(`/previews/${id}/access`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", signal: controller.signal,
    }).then(grant => {
      if (!disposed && previewReady(store.getSnapshot(), id)) { setFrameLoaded(false); setUrl(grant.url); }
    }).catch(() => {
      if (!disposed) setError("预览暂时无法打开，请刷新重试");
    });
    return () => { disposed = true; controller.abort(); };
  }, [store, id, active, visible, revision]);
  // Terminal/revoked state removes the frame immediately; a late access grant cannot restore it.
  const showFrame = active && !!url;
  const refresh = () => { setUrl(""); setError(""); setRevision(value => value + 1); void store.refresh(true); };
  const title = detail?.preview.name || "成果预览";
  const close = onClose ?? (() => {
    window.close();
    if (!window.closed) window.location.assign(detail?.conversation_id
      ? `/?conversation=${encodeURIComponent(detail.conversation_id)}` : "/?chat=1");
  });
  const previewError = active ? error : "";
  const ended = computer?.terminal || detail?.preview.terminal || state.previewErrors[id] === "预览已结束";
  const summary = ended ? "预览已结束"
    : computer && (state.ending[computer.id] || computer.status === "stopping")
    ? "正在结束运行"
    : state.revoked || state.previewErrors[id] ? "预览状态待确认"
    : (detail && !sameComputer ? "此预览已结束" : detail?.preview.summary) || "正在获取预览状态";
  return <section className={`cloud-preview-surface ${standalone ? "cloud-preview-standalone" : ""}`} aria-label="成果预览">
    <header className="cloud-preview-heading">
      <Monitor size={17} aria-hidden="true" /><h2 title={title}>{title}</h2>
      <div className="cloud-preview-actions">
        <Tooltip content="刷新预览"><button type="button" className="icon-button" aria-label="刷新预览" onClick={refresh}><RefreshCw size={16} /></button></Tooltip>
        {computer && <EndCloudComputer computerId={computer.id} />}
        {!standalone && <Tooltip content="新窗口打开"><a className="icon-button" aria-label="新窗口打开预览" href={`/previews/${id}`} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} /></a></Tooltip>}
        {(onClose || standalone) && <Tooltip content="关闭预览"><button type="button" className="icon-button" aria-label="关闭预览"
          onClick={close}><X size={17} /></button></Tooltip>}
      </div>
    </header>
    {active && <p className="cloud-preview-notice"><Link href="/settings/billing" target="_blank" rel="noopener noreferrer">预览运行期间计费</Link></p>}
    <div className="cloud-preview-status"><CloudStatusError computerId={computer?.id} previewId={id} />
      {previewError && <div className="cloud-preview-error" role="status">{previewError}</div>}
    </div>
    <div className="cloud-preview-canvas">
      {showFrame ? <>
        {!frameLoaded && <div className="cloud-preview-loading" role="status"><Loader2 size={20} className="spin" />正在打开预览</div>}
        <iframe key={url} src={url} title={title} referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-forms allow-same-origin allow-downloads" onLoad={() => setFrameLoaded(true)} />
      </> : <div className="cloud-preview-empty" role="status"><Monitor size={28} /><p>{previewError || summary}</p>
        {computer?.stop_reason_text && <p className="cloud-preview-stop-reason">{computer.stop_reason_text}</p>}
        {ended
          && <Link className="cloud-files-link" href="/resources"><FolderOpen size={15} />查看文件</Link>}
      </div>}
    </div>
  </section>;
}

export function PreviewResultAction({ id, name }: { id: string; name: string }) {
  const { store, state } = useCloudComputer();
  const actionId = useId();
  useEffect(() => store.watchPreview(id), [store, id]);
  const ready = previewReady(state, id);
  const detail = state.details[id];
  return <button id={actionId} type="button" className="cloud-result-action" aria-label={`${ready ? "打开预览" : "查看预览状态"} · ${name}`}
    title={name} onClick={event => openPreview(id, event.currentTarget)}>
    <ExternalLink size={15} aria-hidden="true" /><span>{ready ? "打开预览" : "查看预览状态"}</span>
    {!ready && detail?.preview.terminal && <small>已结束</small>}
  </button>;
}

export function ConversationPreview() {
  const { store } = useCloudComputer();
  const [opened, setOpened] = useState<string | null>(null);
  const [narrow, setNarrow] = useState(false);
  const [restoreFocusSelector, setRestoreFocusSelector] = useState<string>();
  const trigger = useRef<HTMLElement | null>(null);
  const close = useCallback(() => {
    setOpened(null);
    requestAnimationFrame(() => { if (trigger.current?.isConnected) trigger.current.focus(); });
  }, []);
  useEffect(() => {
    const show = (event: Event) => {
      const detail = (event as CustomEvent<{ id: string; trigger?: HTMLElement }>).detail;
      trigger.current = detail.trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
      setRestoreFocusSelector(trigger.current?.id ? `#${CSS.escape(trigger.current.id)}` : undefined);
      setOpened(detail.id);
    };
    const resize = () => setNarrow(window.innerWidth < 1000);
    resize(); window.addEventListener("resize", resize); window.addEventListener("zhiku:preview", show);
    const revoke = () => setOpened(null);
    window.addEventListener("rag-session-expired", revoke); window.addEventListener("rag-session-ended", revoke);
    return () => {
      window.removeEventListener("resize", resize); window.removeEventListener("zhiku:preview", show);
      window.removeEventListener("rag-session-expired", revoke); window.removeEventListener("rag-session-ended", revoke);
    };
  }, [store]);
  useEffect(() => {
    if (!opened || narrow) return;
    document.body.classList.add("has-cloud-preview");
    document.querySelector<HTMLButtonElement>(".cloud-preview-dock button")?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented
        && !document.querySelector('[role="dialog"][data-state="open"]')) close();
    };
    window.addEventListener("keydown", escape);
    return () => { document.body.classList.remove("has-cloud-preview"); window.removeEventListener("keydown", escape); };
  }, [opened, narrow, close]);
  return opened ? narrow
    ? <Dialog title="成果预览" fullScreen onClose={close} restoreFocusSelector={restoreFocusSelector}><PreviewSurface key={opened} id={opened} onClose={close} /></Dialog>
    : createPortal(<aside className="cloud-preview-dock"><PreviewSurface key={opened} id={opened} onClose={close} /></aside>, document.body)
    : null;
}
