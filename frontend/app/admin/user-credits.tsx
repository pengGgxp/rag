"use client";

import { useState } from "react";
import { Coins } from "lucide-react";
import { Dialog } from "../../components/ui/dialog";
import { ApiError, request } from "../api";
import { validCredits } from "../credit-format";
import { useAdminChanges } from "./admin-changes";
import { AdminLoad, date, json, number, useAdminResource, type Credits } from "./admin-ui";

type Adjustment = {
  id: string;
  amount: number;
  balance_after: number;
  reason: string;
  actor_name: string | null;
  created_at: string;
};
type Balance = { credits: Credits; pending_credits: number; adjustments: Adjustment[] };
type Payload = {
  request_id: string;
  direction: "add" | "deduct";
  amount: number;
  reason: string;
  expected_balance: number;
};

export function UserCredits({ id, name, changed }: {
  id: string;
  name: string;
  changed: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = `user-credits-${id}`;
  return <div className="admin-actions admin-credit-actions">
    <button id={trigger} className="secondary" onClick={() => setOpen(true)}>
      <Coins size={16} />调整积分
    </button>
    {open && <CreditEditor id={id} name={name} trigger={trigger}
      close={() => setOpen(false)} changed={changed} />}
  </div>;
}

function CreditEditor({ id, name, trigger, close, changed }: {
  id: string;
  name: string;
  trigger: string;
  close: () => void;
  changed: (notice: string) => void;
}) {
  const path = `/site/admin/users/${encodeURIComponent(id)}/credits`;
  const { data, loading, error, reload } = useAdminResource<Balance>(path);
  const [direction, setDirection] = useState<Payload["direction"]>("add");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [conflict, setConflict] = useState(false);
  const [attempt, setAttempt] = useState<Payload | null>(null);
  const dirty = Boolean(amount || reason);
  useAdminChanges(dirty || Boolean(attempt), busy);
  const value = /^\d+(?:\.\d{1,4})?$/.test(amount) ? Number(amount) : NaN;
  const after = (Math.round((data?.credits.balance ?? 0) * 10000) +
    (direction === "add" ? 1 : -1) * Math.round(value * 10000)) / 10000;
  const valid = validCredits(value) && value > 0 && Boolean(reason.trim()) && after >= 0;

  function askClose() {
    if (attempt) setFailure("提交结果尚未确认，请重试确认。");
    else if (dirty) setDiscard(true);
    else close();
  }

  async function submit() {
    if (!data || busy || (!attempt && (!valid || conflict))) return;
    if (!confirming && !attempt) { setConfirming(true); return; }
    const payload = attempt ?? { request_id: crypto.randomUUID(), direction, amount: value,
      reason: reason.trim(), expected_balance: data.credits.balance };
    setAttempt(payload);
    setBusy(true);
    setFailure("");
    try {
      const result = await request<{ adjustment: Adjustment }>(`${path}/adjustments`, json(payload));
      changed(`积分已调整，余额 ${number(result.adjustment.balance_after)} 积分`);
      close();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "暂时无法确认调整结果，请重试确认");
      if (error instanceof ApiError && error.status < 500) {
        setAttempt(null);
        setConfirming(false);
        setConflict(error.status === 409);
      }
    } finally {
      setBusy(false);
    }
  }

  return <Dialog title="调整账号积分" busy={busy} restoreFocusSelector={`#${trigger}`} onClose={askClose}>
    <div className="admin-confirm admin-credit-editor">
      <div className="admin-credit-target"><strong>{name}</strong></div>
      <AdminLoad loading={loading} error={error} retry={reload}>
        {data && <>
          <dl className="admin-credit-preview">
            <div><dt>当前余额</dt><dd>{number(data.credits.balance)} 积分</dd></div>
            {Number(data.pending_credits) > 0 && <div><dt>待结算</dt><dd>{number(data.pending_credits)} 积分</dd></div>}
          </dl>
          <form className="admin-credit-form" onSubmit={event => { event.preventDefault(); void submit(); }}>
            <fieldset className="provider-form-fields" disabled={busy || confirming || Boolean(attempt)}>
              <label>调整方式<select value={direction} onChange={event => setDirection(event.target.value as Payload["direction"])}>
                <option value="add">增加积分</option><option value="deduct">扣减积分</option>
              </select></label>
              <label>积分数量<input inputMode="decimal" maxLength={15} required value={amount}
                onChange={event => setAmount(event.target.value)} /></label>
              <label>调整原因<textarea required maxLength={200} rows={2} value={reason}
                onChange={event => setReason(event.target.value)} /></label>
            </fieldset>
            {Number.isFinite(after) && <p aria-live="polite">
              {after < 0 ? "扣减积分不能超过当前余额" : `调整后余额 ${number(after)} 积分`}
            </p>}
            {failure && <p className="form-error" role="alert">{failure}</p>}
            {conflict && <button type="button" className="secondary" onClick={async () => {
              await reload(); setConflict(false);
            }}>刷新余额</button>}
            <div className="admin-actions">
              <button className="primary" disabled={busy || loading || Boolean(error) || (!attempt && (!valid || conflict))}>
                {busy ? "正在提交" : attempt ? "重试确认" : confirming ? "确认调整" : "下一步"}
              </button>
              {confirming && !attempt && <button type="button" className="secondary"
                onClick={() => setConfirming(false)}>返回修改</button>}
            </div>
          </form>
          {discard && <div className="admin-inline-confirm" role="alert">
            <span>放弃尚未提交的调整？</span>
            <button className="secondary" onClick={() => setDiscard(false)}>继续编辑</button>
            <button className="danger" onClick={close}>放弃修改</button>
          </div>}
          <details className="admin-credit-history">
            <summary>最近调整</summary>
            {!data.adjustments.length && <p>暂无调整记录</p>}
            {data.adjustments.map(entry => <div key={entry.id} className="admin-credit-entry">
              <strong>{entry.amount > 0 ? "+" : ""}{number(entry.amount)} 积分</strong>
              <p>{entry.reason}</p><small>{entry.actor_name || "管理员"} · {date(entry.created_at)}</small>
            </div>)}
          </details>
        </>}
      </AdminLoad>
    </div>
  </Dialog>;
}
