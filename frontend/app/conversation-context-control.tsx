"use client";

import { useEffect, useRef, useState } from "react";
import { Check, CircleAlert, CircleGauge, Loader2, Minimize2, RefreshCw, X } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";

export type ConversationContextState = {
  estimatedInputTokens: number | null;
  capacity: number | null;
  outputReserve: number | null;
  eligible: boolean;
  status: "idle" | "running" | "completed" | "failed";
  error?: string;
  windowId?: string | null;
  contextRevision?: number;
};

const formatK = (value: number) => `${(value / 1000).toLocaleString("zh-CN", {
  maximumFractionDigits: value < 1000 ? 3 : 1,
})}k`;

export function ConversationContextControl({ state, disabled, onCompact, loading = false, onRetry,
  modelSelected = true, loadError = "", compactRequest = 0 }: {
  compactRequest?: number;
  state: ConversationContextState;
  disabled: boolean;
  onCompact: () => void;
  loading?: boolean;
  onRetry?: () => void;
  modelSelected?: boolean;
  loadError?: string;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const ready = !loading && modelSelected && !loadError && state.capacity !== null && state.outputReserve !== null;
  const usableInput = ready ? Math.max(0, state.capacity! - state.outputReserve!) : null;
  const percent = ready && state.capacity! > 0 && state.estimatedInputTokens !== null
    ? Math.min(100, Math.ceil(state.estimatedInputTokens / state.capacity! * 100)) : null;
  useEffect(() => {
    if (compactRequest) { setOpen(true); setConfirm(true); }
  }, [compactRequest]);
  const busy = state.status === "running";
  const over = percent !== null && percent >= 90;
  const close = () => { setOpen(false); setConfirm(false); };
  return <>
    <Tooltip content={busy ? "正在整理上下文" : loading ? "正在读取上下文状态" : loadError ? "上下文状态读取失败"
      : !modelSelected ? "请先选择模型" : !ready ? "上下文用量暂不可用"
      : percent === null ? "上下文" : `上下文已用 ${percent}%`}>
      <button ref={trigger} type="button" className={`context-trigger ${over ? "context-near-limit" : ""}`}
        aria-label="上下文" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setOpen(value => !value); setConfirm(false); }}>
        {busy || loading ? <Loader2 size={17} className="spin" aria-hidden="true" /> : <CircleGauge size={17} aria-hidden="true" />}
        {percent !== null && <span>已用 {percent}%</span>}
      </button>
    </Tooltip>
    {open && <Dialog title="上下文" anchor={trigger.current} onClose={close}>
      <div className="context-panel">
        <div className="context-panel-heading">
          <strong>上下文</strong>
          <button type="button" className="icon-button" aria-label="关闭上下文" onClick={close}><X size={16} /></button>
        </div>
        {loading ? <p className="context-status" role="status">正在读取上下文状态…</p>
        : loadError ? <p className="context-status context-warning" role="alert"><CircleAlert size={15} />{loadError}</p>
        : !modelSelected ? <p className="context-status">请先选择模型，再查看上下文状态。</p>
        : ready ? <>
          {percent === null ? <p className="context-status">发送消息后查看用量</p> : <>
            <div className="context-budget"><strong>已用 {percent}%</strong></div>
            <progress className={over ? "context-progress near-limit" : "context-progress"}
              aria-label="上下文已用比例" max={100} value={percent} />
          </>}
          <p className="context-note">共 {formatK(state.capacity!)} · 已用 {state.estimatedInputTokens === null ? "—" : formatK(state.estimatedInputTokens)}</p>
        </> : <p className="context-status context-warning"><CircleAlert size={15} />暂时无法查看用量，请稍后重试。</p>}
        {state.status === "completed" && <p className="context-status"><Check size={14} />上下文已整理</p>}
        {state.status === "failed" && <p className="context-status context-warning" role="alert"><CircleAlert size={14} />{state.error || "整理失败，原始上下文已保留"}</p>}
        {(loadError || state.status === "failed") && onRetry && <button type="button" className="text-button" onClick={onRetry}><RefreshCw size={14} />重新加载</button>}
        <div className="context-panel-footer">
          {confirm && !busy ? <div className="context-confirm">
            <p>压缩较早的对话以腾出空间，完整记录仍保留。此次操作按当前模型用量计费。</p>
            <div><button type="button" className="text-button" onClick={() => setConfirm(false)}>取消</button>
              <button type="button" className="primary" disabled={disabled || !state.eligible || !ready}
                onClick={() => { setConfirm(false); setOpen(false); onCompact(); }}>确认压缩</button></div>
          </div> : <button type="button" className="context-compact" disabled={disabled || busy || !state.eligible || !ready}
            onClick={() => setConfirm(true)}>
            {busy ? <Loader2 size={15} className="spin" /> : <Minimize2 size={15} />}
            {busy ? "正在压缩…" : "压缩上下文"}
          </button>}
          {ready && !state.eligible && !busy && <span className="context-note">暂无需要整理的历史内容</span>}
        </div>
      </div>
    </Dialog>}
  </>;
}
