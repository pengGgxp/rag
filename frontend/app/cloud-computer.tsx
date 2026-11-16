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

export function CloudFees({ computerId }: { computerId?: string }) {
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
  const computer = computerId ? state.computers[computerId] : null;
  const seconds = computer?.seconds ?? 0;
  return <>
    <button ref={trigger} id={triggerId} type="button" className="cloud-fees-trigger" aria-haspopup="dialog" aria-expanded={open}
      aria-label={`费用详情 · ${computer?.name || "云电脑"}`}
      onClick={() => setOpen(value => !value)}><Info size={14} aria-hidden="true" />费用详情</button>
    {open && <Dialog title={computer ? `${computer.name} · 费用` : "云电脑费用"} anchor={mobile ? undefined : trigger.current}
      restoreFocusSelector={`[id="${triggerId}"]`} onClose={() => setOpen(false)}>
      <div className="cloud-fees">
        {!mobile && <div className="cloud-fees-heading"><h3 ref={heading} tabIndex={-1}>{computer?.name || "云电脑"}</h3>
          <button type="button" className="icon-button" aria-label="关闭费用详情" onClick={() => setOpen(false)}><X size={16} /></button></div>}
        {state.revoked ? <p>暂不可查看，请重新登录。</p> : <>
          <div className="cloud-fees-total"><span>{computer?.terminal ? "最终消费" : "当前累计"}</span>
            <strong>{computer ? formatCredits(Number(computer.credits)) : "0"}<small>积分</small></strong></div>
          <dl className="cloud-fees-rows">
            <div><dt>{computer ? "本次单价" : "启动单价"}</dt><dd>{formatCredits(Number(computer?.credits_per_minute ?? state.catalog?.credits_per_minute ?? 0))} 积分/分钟</dd></div>
            <div><dt>已运行</dt><dd>{Math.floor(seconds / 60)} 分 {seconds % 60} 秒</dd></div>
            <div><dt>闲置结束时间</dt><dd>{cloudTime(computer?.idle_ends_at ?? null)}</dd></div>
            <div><dt>最长运行至</dt><dd>{cloudTime(computer?.max_ends_at ?? null)}</dd></div>
            {computer?.stop_reason_text && <div><dt>结束原因</dt><dd>{computer.stop_reason_text}</dd></div>}
            {computer?.stop_reason === "insufficient_balance" && computer.renewal_required_credits != null
              && <div><dt>续期所需预留</dt><dd>{formatCredits(Number(computer.renewal_required_credits))} 积分</dd></div>}
          </dl>
          <p className="cloud-fees-note">{computer?.terminal ? "本次费用已结算。" : computer ? "按秒累计，包含等待时间。" : "尚未启动，不产生云电脑费用。"}
            {!computer && state.catalog?.idle_seconds ? ` 无操作 ${Math.ceil(state.catalog.idle_seconds / 60)} 分钟后自动结束。` : ""}</p>
          {state.error && <p className="cloud-fees-warning"><CircleAlert size={14} />{state.error}</p>}
        </>}
      </div>
    </Dialog>}
  </>;
}

export function EndCloudComputer({ computerId }: { computerId: string }) {
  const { store, state } = useCloudComputer();
  const computer = state.computers[computerId];
  if (!computer || computer.terminal || state.revoked) return null;
  const ending = state.ending[computerId] || computer.status === "stopping";
  return <button type="button" className="cloud-end-button" aria-label={`结束云电脑 · ${computer.name}`}
    disabled={ending} onClick={() => void store.end(computerId)}>
    {ending ? <Loader2 size={14} className="spin" aria-hidden="true" /> : <Power size={14} aria-hidden="true" />}
    {ending ? "正在结束" : "结束云电脑"}
  </button>;
}

export function CloudStatusError({ computerId }: { computerId?: string }) {
  const { store, state } = useCloudComputer();
  const message = (computerId && (state.actionErrors[computerId] || state.instanceErrors[computerId])) || state.error;
  if (!message) return null;
  return <div className="cloud-status-error">
    <CircleAlert size={14} aria-hidden="true" /><span>{message}</span>
    {!state.revoked && <Tooltip content="刷新云电脑状态"><button type="button" className="icon-button" aria-label="刷新云电脑状态"
      onClick={() => void store.refresh(true)}><RefreshCw size={14} /></button></Tooltip>}
  </div>;
}

function ComputerRow({ computerId }: { computerId: string }) {
  const { store, state } = useCloudComputer();
  const computer = state.computers[computerId];
  const commands = computer && !computer.terminal ? state.commands.filter(item => item.computer_id === computer.id && !item.terminal) : [];
  const ending = state.ending[computerId] || computer?.status === "stopping";
  const working = computer && !computer.terminal && ["starting", "running", "uncertain"].includes(computer.status);
  return <section className="cloud-computer-instance" aria-label={computer?.name || "云电脑"}>
      <div className="cloud-computer-line">
        <span className={`cloud-computer-symbol ${computer && !computer.terminal ? "is-active" : ""}`}>
          {ending || working ? <Loader2 size={18} className="spin" aria-hidden="true" /> : <Monitor size={18} aria-hidden="true" />}
        </span>
        <div className="cloud-computer-copy">
          <span className="cloud-instance-name">{computer?.name} · {computerId.slice(0, 8)}</span>
          <strong role="status">{ending ? "正在结束云电脑" : computer?.summary || (state.revoked ? "云电脑暂不可访问" : state.error ? "云电脑状态待确认" : "云电脑待命")}</strong>
          <span>{computer?.terminal ? `本次消费 ${formatCredits(Number(computer.credits))} 积分`
            : ending ? computer?.stop_reason_text || "正在确认结束结果"
            : computer ? computer.idle_ends_at ? `无操作将于 ${cloudTime(computer.idle_ends_at)} 结束` : "运行期间持续计费"
            : state.error ? "请刷新后重试" : "执行任务时按需启动"}</span>
          {computer?.terminal && computer.stop_reason_text && <span>{computer.stop_reason_text}</span>}
        </div>
        <div className="cloud-computer-controls">
          {!state.revoked && (computer || state.catalog?.available) && <CloudFees computerId={computerId} />}
          {computer?.terminal ? <Link className="cloud-files-link" href="/resources"><FolderOpen size={14} />查看文件</Link> : <EndCloudComputer computerId={computerId} />}
        </div>
      </div>
      {!ending && commands.map(command => <div className="cloud-command-line" key={command.id}><span>命令 {command.id.slice(0, 8)} 执行中</span>
        <button type="button" className="cloud-command-stop" aria-label={`停止命令 ${command.id.slice(0, 8)} · ${computer.name}`}
          disabled={!!state.cancelling[command.id] || command.cancel_requested}
          onClick={() => void store.cancel(command.id)}><Square size={12} aria-hidden="true" />{command.cancel_requested ? "正在停止命令" : "停止当前命令"}</button>
      </div>)}
      <CloudStatusError computerId={computerId} />
    </section>;
}

export function CloudComputerPanel() {
  const { state } = useCloudComputer();
  const active = state.computerIds.filter(id => !state.computers[id]?.terminal).length;
  return <>
    <ConversationPreview />
    {(state.catalog?.available || state.computerIds.length > 0 || state.error) && <section className="cloud-computer-panel" aria-label="云电脑">
      {state.computerIds.length > 1 ? <details className="cloud-computer-list">
        <summary>{active ? `${active} 台云电脑运行或核对中` : "云电脑已结束"} · 查看各台状态</summary>
        <div className="cloud-computer-rows">{state.computerIds.map(id => <ComputerRow key={id} computerId={id} />)}</div>
      </details> : state.computerIds.length === 1 ? <ComputerRow computerId={state.computerIds[0]} /> :
        <div className="cloud-computer-line"><Monitor size={18} aria-hidden="true" /><div className="cloud-computer-copy">
          <strong role="status">{state.revoked ? "云电脑暂不可访问" : "云电脑待命"}</strong><span>执行任务时按需创建</span>
        </div><CloudFees /><CloudStatusError /></div>}
    </section>}
  </>;
}
