"use client";

import { useEffect, useId, useState } from "react";
import { CircleAlert, Loader2, Power, RefreshCw } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { useCloudComputer } from "./cloud-computer-context";
import "./cloud-computer.css";

export function EndCloudComputer({ computerId }: { computerId: string }) {
  const { store, state } = useCloudComputer();
  const [confirm, setConfirm] = useState(false);
  const triggerId = useId();
  useEffect(() => {
    if (!confirm) return;
    // The nested confirmation consumes Escape before the fullscreen preview.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      event.preventDefault(); event.stopImmediatePropagation(); setConfirm(false);
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [confirm]);
  const computer = state.computers[computerId];
  if (!computer || computer.terminal || state.revoked) return null;
  const ending = state.ending[computerId] || computer.status === "stopping";
  return <>
    <button id={triggerId} type="button" className="cloud-end-button" disabled={ending}
      onClick={() => setConfirm(true)}>
      {ending ? <Loader2 size={14} className="spin" aria-hidden="true" /> : <Power size={14} aria-hidden="true" />}
      {ending ? "正在结束" : "结束运行"}
    </button>
    {confirm && <Dialog title="结束运行？" restoreFocusSelector={`[id="${triggerId}"]`} onClose={() => setConfirm(false)}>
      <div className="cloud-end-confirm">
        <p>此预览将停止，已保存的文件会保留。</p>
        <div className="dialog-actions">
          <button type="button" onClick={() => setConfirm(false)}>继续预览</button>
          <button type="button" className="cloud-end-button" onClick={() => { setConfirm(false); void store.end(computerId); }}>
            <Power size={15} aria-hidden="true" />结束运行
          </button>
        </div>
      </div>
    </Dialog>}
  </>;
}

export function CloudStatusError({ computerId, previewId }: { computerId?: string; previewId: string }) {
  const { store, state } = useCloudComputer();
  const message = (computerId && state.actionErrors[computerId]) || state.previewErrors[previewId] || state.error;
  if (!message || message === "预览已结束") return null;
  return <div className="cloud-status-error">
    <CircleAlert size={14} aria-hidden="true" /><span>{message}</span>
    {!state.revoked && <Tooltip content="刷新预览状态"><button type="button" className="icon-button" aria-label="刷新预览状态"
      onClick={() => void store.refresh(true)}><RefreshCw size={14} /></button></Tooltip>}
  </div>;
}
