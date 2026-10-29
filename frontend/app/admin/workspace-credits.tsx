"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp, Check, Coins, Loader2 } from "lucide-react";
import { Dialog } from "../../components/ui/dialog";
import { ApiError, request } from "../api";
import { useAdminChanges } from "./admin-changes";
import { AdminLoad, CreditSummary, date, json, number, useAdminResource, type Credits } from "./admin-ui";

type Adjustment = {
  id: string; amount: number; balance_after: number; reason: string;
  actor_name: string | null; created_at: string;
};
type Balance = { credits: Credits; pending_credits: number; adjustments: Adjustment[] };
type Payload = { request_id: string; direction: "add" | "deduct"; amount: number; reason: string; expected_balance: number };

// A shared entry point for user membership rows and workspace details, not a separate balance owner.
export function WorkspaceCredits({ id, name, credits, changed }: {
  id: string; name: string; credits: Credits; changed: (notice: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [adjusted, setAdjusted] = useState<Credits | null>(null);
  const trigger = `workspace-credits-${id}`;
  return <>
    <CreditSummary credits={adjusted ?? credits} />
    <div className="admin-actions admin-credit-actions">
      <button id={trigger} className="secondary" onClick={() => setOpen(true)}><Coins size={16} />调整积分</button>
    </div>
    {open && <CreditEditor id={id} name={name} trigger={trigger} close={() => setOpen(false)}
      changed={(balance, notice) => { setAdjusted(balance); changed(notice); }} />}
  </>;
}

function CreditEditor({ id, name, trigger, close, changed }: {
  id: string; name: string; trigger: string; close: () => void; changed: (credits: Credits, notice: string) => void;
}) {
  const path = `/site/admin/workspaces/${encodeURIComponent(id)}/credits`;
  const { data, loading, error, reload } = useAdminResource<Balance>(path);
  const [direction, setDirection] = useState<"add" | "deduct">("add");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [conflict, setConflict] = useState(false);
  const [attempt, setAttempt] = useState<Payload | null>(null);
  const dirty = !!amount || !!reason;
  useAdminChanges(dirty || !!attempt, busy);
  const value = /^[1-9]\d*$/.test(amount) ? Number(amount) : NaN;
  const valid = Number.isSafeInteger(value) && value <= 2_147_483_647 && reason.trim().length > 0;
  const after = (data?.credits.balance ?? 0) + (direction === "add" ? value : -value);
  function askClose() {
    if (attempt) { setFailure("上次提交结果尚未确认，请重试确认，避免重复调整。"); return; }
    if (dirty) setDiscard(true); else close();
  }
  async function submit() {
    if (!data || busy || (!attempt && (!valid || conflict))) return;
    const payload = attempt ?? { request_id: crypto.randomUUID(), direction, amount: value,
      reason: reason.trim(), expected_balance: data.credits.balance };
    setAttempt(payload); setBusy(true); setFailure("");
    try {
      const result = await request<{ credits: Credits; adjustment: Adjustment }>(`${path}/adjustments`, json(payload));
      setAttempt(null);
      changed(result.credits, `${name}已${result.adjustment.amount > 0 ? "增加" : "扣减"} ${number(Math.abs(result.adjustment.amount))} 积分`);
      close();
    } catch (e) {
      setFailure(e instanceof Error ? e.message : "暂时无法确认调整结果，请重试确认");
      if (e instanceof ApiError && e.status < 500) {
        setAttempt(null); setConfirming(false);
        if (e.status === 409) setConflict(true);
      }
    } finally { setBusy(false); }
  }
  return <Dialog title="调整工作区积分" busy={busy} restoreFocusSelector={`#${trigger}`} onClose={askClose}>
    <div className="admin-confirm admin-credit-editor">
      <div className="admin-credit-target"><strong>{name}</strong><span>工作区共享积分</span></div>
      <AdminLoad loading={loading} error={error} retry={reload}>
        {data && <>
          <dl className="admin-credit-preview"><div><dt>当前余额</dt><dd>{number(data.credits.balance)} 积分</dd></div>
            {Boolean(data.pending_credits) && <div><dt>待结算</dt><dd>{number(data.pending_credits)} 积分</dd></div>}</dl>
          <form className="admin-credit-form" onSubmit={e => {
            e.preventDefault();
            if (confirming || attempt) void submit();
            else if (valid && after >= 0 && !conflict) { setFailure(""); setConfirming(true); }
          }}>
            <fieldset className="provider-form-fields" disabled={busy || confirming || !!attempt}>
              <label>调整方式<select value={direction} onChange={e => setDirection(e.target.value as "add" | "deduct")}>
                <option value="add">增加积分</option><option value="deduct">扣减积分</option>
              </select></label>
              <label>积分数量<input autoFocus inputMode="numeric" type="text" pattern="[1-9][0-9]*" required
                maxLength={10} value={amount} onChange={e => setAmount(e.target.value)} /></label>
              <label>调整原因<textarea required maxLength={200} rows={2} value={reason} onChange={e => setReason(e.target.value)} /></label>
            </fieldset>
            {valid && <div className="admin-credit-preview" aria-live="polite"><span>调整后余额</span>
              <strong className={after < 0 ? "admin-danger" : ""}>{number(after)} 积分</strong></div>}
            {valid && after < 0 && <p className="form-error">扣减积分不能超过当前余额</p>}
            {confirming && <p className="admin-credit-confirmation">确认{direction === "add" ? "增加" : "扣减"} {number(value)} 积分？
              此次调整将影响该工作区的所有成员。</p>}
            {failure && <p className="form-error" role="alert">{failure}</p>}
            {conflict && <button type="button" className="secondary" disabled={busy || loading}
              onClick={async () => { await reload(); setConflict(false); setConfirming(false); }}>刷新余额</button>}
            {attempt && !busy && <p className="billing-muted">上次提交结果尚未确认，请重试确认。</p>}
            <div className="admin-actions">
              <button className={direction === "deduct" && confirming ? "danger" : "primary"}
                disabled={busy || loading || !!error || (!attempt && (!valid || after < 0 || conflict))}>
                {busy ? <Loader2 size={16} className="spin" /> : confirming || attempt ? <Check size={16} />
                  : direction === "add" ? <ArrowUp size={16} /> : <ArrowDown size={16} />}
                {busy ? "正在提交" : attempt ? "重试确认" : confirming ? "确认调整" : "下一步"}
              </button>
              {confirming && !attempt && <button type="button" className="secondary" disabled={busy}
                onClick={() => setConfirming(false)}>返回修改</button>}
            </div>
          </form>
          {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未提交的调整？</span>
            <button className="secondary" onClick={() => setDiscard(false)}>继续编辑</button>
            <button className="danger" onClick={close}>放弃修改</button></div>}
          <details className="admin-credit-history"><summary>最近调整{data.adjustments.length ? ` · ${data.adjustments.length}` : ""}</summary>
            {!data.adjustments.length && <p className="billing-muted">暂无调整记录</p>}
            {data.adjustments.map(entry => <div key={entry.id} className="admin-credit-entry">
              <div><strong>{entry.amount > 0 ? "+" : ""}{number(entry.amount)} 积分</strong>
                <span>余额 {number(entry.balance_after)}</span></div>
              <p>{entry.reason}</p><small>{entry.actor_name || "管理员"} · {date(entry.created_at)}</small>
            </div>)}
          </details>
        </>}
      </AdminLoad>
    </div>
  </Dialog>;
}
