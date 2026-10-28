"use client";

import { useRef, useState } from "react";
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
};

const number = (value: number) => value.toLocaleString("zh-CN");

export function ConversationContextControl({ state, disabled, onCompact, loading = false, onRetry,
  modelSelected = true, loadError = "" }: {
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
  const usable = ready ? Math.max(0, state.capacity! - state.outputReserve! - Math.ceil(state.capacity! * 0.05)) : null;
  const percent = usable && usable > 0 && state.estimatedInputTokens !== null
    ? Math.ceil(state.estimatedInputTokens / usable * 100) : null;
  const busy = state.status === "running";
  const over = percent !== null && percent >= 75;
  const close = () => { setOpen(false); setConfirm(false); };
  return <>
    <Tooltip content={busy ? "正在整理上下文" : loading ? "正在读取上下文状态" : loadError ? "上下文状态读取失败"
      : !modelSelected ? "请先选择模型" : !ready ? "上下文保护未就绪"
      : percent === null ? "上下文保护已就绪" : `上下文估算 · ${percent}% 可用输入`}>
      <button ref={trigger} type="button" className={`context-trigger ${over ? "context-near-limit" : ""}`}
        aria-label="上下文" aria-haspopup="dialog" aria-expanded={open} onClick={() => { setOpen(value => !value); setConfirm(false); }}>
        {busy || loading ? <Loader2 size={17} className="spin" aria-hidden="true" /> : <CircleGauge size={17} aria-hidden="true" />}
        {percent !== null && <span>{percent}%</span>}
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
        : ready && usable ? <>
          {state.estimatedInputTokens === null ? <p className="context-status">上下文保护已就绪，发送问题后显示输入估算。</p> : <>
            <div className="context-budget"><strong>{number(state.estimatedInputTokens)}</strong><span> / {number(usable)} Token</span><small>估算输入</small></div>
            <progress className={over ? "context-progress near-limit" : "context-progress"}
              aria-label="估算输入占可用预算比例" max={usable} value={Math.min(state.estimatedInputTokens, usable)} />
          </>}
          <dl className="context-breakdown">
            <div><dt>模型容量</dt><dd>{number(state.capacity!)} Token</dd></div>
            <div><dt>输出预留</dt><dd>{number(state.outputReserve!)} Token</dd></div>
            <div><dt>安全余量</dt><dd>{number(Math.ceil(state.capacity! * 0.05))} Token</dd></div>
          </dl>
          <p className="context-note">达到可用输入预算的 75% 时自动整理。保留近期原文，完整记录仍可查看。</p>
        </> : <p className="context-status context-warning"><CircleAlert size={15} />上下文保护未就绪，请联系管理员配置模型容量。</p>}
        {state.status === "completed" && <p className="context-status"><Check size={14} />上下文已整理</p>}
        {state.status === "failed" && <p className="context-status context-warning" role="alert"><CircleAlert size={14} />{state.error || "整理失败，原始上下文已保留"}</p>}
        {(loadError || state.status === "failed") && onRetry && <button type="button" className="text-button" onClick={onRetry}><RefreshCw size={14} />重新加载</button>}
        <div className="context-panel-footer">
          <p className="context-note">整理使用当前模型，按实际 Token 计费，无额外次数费。</p>
          {confirm && !busy ? <div className="context-confirm">
            <p>立即整理并计入本工作区用量？</p>
            <div><button type="button" className="text-button" onClick={() => setConfirm(false)}>取消</button>
              <button type="button" className="primary" disabled={disabled || !state.eligible || !ready}
                onClick={() => { setConfirm(false); onCompact(); }}>确认整理</button></div>
          </div> : <button type="button" className="context-compact" disabled={disabled || busy || !state.eligible || !ready}
            onClick={() => setConfirm(true)}>
            {busy ? <Loader2 size={15} className="spin" /> : <Minimize2 size={15} />}
            {busy ? "正在整理…" : "立即整理"}
          </button>}
          {ready && !state.eligible && !busy && <span className="context-note">暂无需要整理的历史内容</span>}
        </div>
      </div>
    </Dialog>}
  </>;
}
