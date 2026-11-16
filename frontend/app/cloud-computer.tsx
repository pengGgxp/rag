"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CircleAlert, FolderOpen, Info, Loader2, Monitor, Power, RefreshCw, Square, X } from "lucide-react";
import Link from "next/link";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { formatCredits } from "./credit-format";
import { useCloudComputer } from "./cloud-computer-context";
import { ConversationPreview } from "./cloud-preview";
import "./cloud-computer.css";

export function cloudTime(value: string | null) {
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "—";
}

export function CloudFees() {
  const { state } = useCloudComputer();
  const [open, setOpen] = useState(false);
  const [mobile, setMobile] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const triggerId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 700px)");
    const update = () => setMobile(query.matches);
    update(); query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (open && !mobile) {
      const frame = requestAnimationFrame(() => heading.current?.focus());
      return () => cancelAnimationFrame(frame);
    }
  }, [open, mobile]);
  useEffect(() => {
    if (!open) return;
    // Consume Escape before the enclosing fullscreen preview handles the same key.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      event.preventDefault(); event.stopImmediatePropagation(); setOpen(false);
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [open]);
  const computer = state.computer;
  const seconds = computer?.seconds ?? 0;
  return <>
    <button ref={trigger} id={triggerId} type="button" className="cloud-fees-trigger" aria-haspopup="dialog" aria-expanded={open}
      onClick={() => setOpen(value => !value)}><Info size={14} aria-hidden="true" />费用详情</button>
    {open && <Dialog title="云电脑费用" anchor={mobile ? undefined : trigger.current}
      restoreFocusSelector={`[id="${triggerId}"]`} onClose={() => setOpen(false)}>
      <div className="cloud-fees">
        {!mobile && <div className="cloud-fees-heading"><h3 ref={heading} tabIndex={-1}>本次云电脑</h3>
          <button type="button" className="icon-button" aria-label="关闭费用详情" onClick={() => setOpen(false)}><X size={16} /></button></div>}
        {state.revoked ? <p>暂不可查看，请重新登录。</p> : <>
          <div className="cloud-fees-total"><span>{computer?.terminal ? "最终消费" : "当前累计"}</span>
            <strong>{computer ? formatCredits(Number(computer.credits)) : "0"}<small>积分</small></strong></div>
          <dl className="cloud-fees-rows">
            <div><dt>{computer ? "本次单价" : "启动单价"}</dt><dd>{formatCredits(Number(computer?.credits_per_minute ?? state.catalog?.credits_per_minute ?? 0))} 积分/分钟</dd></div>
            <div><dt>已运行</dt><dd>{Math.floor(seconds / 60)} 分 {seconds % 60} 秒</dd></div>
            <div><dt>闲置结束时间</dt><dd>{cloudTime(computer?.idle_ends_at ?? null)}</dd></div>
            <div><dt>最长运行至</dt><dd>{cloudTime(computer?.max_ends_at ?? null)}</dd></div>
          </dl>
          <p className="cloud-fees-note">{computer?.terminal ? "本次费用已结算。" : computer ? "按秒累计，包含等待时间。" : "尚未启动，不产生云电脑费用。"}
            {!computer && state.catalog?.idle_seconds ? ` 无操作 ${Math.ceil(state.catalog.idle_seconds / 60)} 分钟后自动结束。` : ""}</p>
          {state.error && <p className="cloud-fees-warning"><CircleAlert size={14} />{state.error}</p>}
        </>}
      </div>
    </Dialog>}
  </>;
}

export function EndCloudComputer() {
  const { store, state } = useCloudComputer();
  if (!state.computer || state.computer.terminal || state.revoked) return null;
  const ending = state.ending || state.computer.status === "stopping";
  return <button type="button" className="cloud-end-button" disabled={ending} onClick={() => void store.end()}>
    {ending ? <Loader2 size={14} className="spin" aria-hidden="true" /> : <Power size={14} aria-hidden="true" />}
    {ending ? "正在结束" : "结束云电脑"}
  </button>;
}

export function CloudStatusError() {
  const { store, state } = useCloudComputer();
  const message = state.actionError || state.error;
  if (!message) return null;
  return <div className="cloud-status-error">
    <CircleAlert size={14} aria-hidden="true" /><span>{message}</span>
    {!state.revoked && <Tooltip content="刷新云电脑状态"><button type="button" className="icon-button" aria-label="刷新云电脑状态"
      onClick={() => void store.refresh(true)}><RefreshCw size={14} /></button></Tooltip>}
  </div>;
}

export function CloudComputerPanel() {
  const { store, state } = useCloudComputer();
  const computer = state.computer;
  const command = !computer?.terminal && state.commands.find(item => !item.terminal);
  const ending = state.ending || computer?.status === "stopping";
  const working = computer && !computer.terminal && ["starting", "running", "uncertain"].includes(computer.status);
  return <>
    <ConversationPreview />
    {(state.catalog?.available || computer || state.error) && <section className="cloud-computer-panel" aria-label="云电脑">
      <div className="cloud-computer-line">
        <span className={`cloud-computer-symbol ${computer && !computer.terminal ? "is-active" : ""}`}>
          {ending || working ? <Loader2 size={18} className="spin" aria-hidden="true" /> : <Monitor size={18} aria-hidden="true" />}
        </span>
        <div className="cloud-computer-copy">
          <strong role="status">{ending ? "正在结束云电脑" : computer?.summary || (state.revoked ? "云电脑暂不可访问" : state.error ? "云电脑状态待确认" : "云电脑待命")}</strong>
          <span>{computer?.terminal ? `本次消费 ${formatCredits(Number(computer.credits))} 积分`
            : ending ? computer?.status === "stopping" && computer.summary !== "正在结束云电脑" ? computer.summary : "正在确认结束结果"
            : computer ? computer.idle_ends_at ? `无操作将于 ${cloudTime(computer.idle_ends_at)} 结束` : "运行期间持续计费"
            : state.error ? "请刷新后重试" : "执行任务时按需启动"}</span>
        </div>
        <div className="cloud-computer-controls">
          {!state.revoked && (computer || state.catalog?.available) && <CloudFees />}
          {computer?.terminal ? <Link className="cloud-files-link" href="/resources"><FolderOpen size={14} />查看文件</Link> : <EndCloudComputer />}
        </div>
      </div>
      {command && !ending && <div className="cloud-command-line"><span>当前命令执行中</span>
        <button type="button" className="cloud-command-stop" disabled={!!state.cancelling || command.cancel_requested}
          onClick={() => void store.cancel(command.id)}><Square size={12} aria-hidden="true" />{command.cancel_requested ? "正在停止命令" : "停止当前命令"}</button>
      </div>}
      <CloudStatusError />
    </section>}
  </>;
}
