"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Loader2, Plus, RefreshCw } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { request } from "./api";
import { useAuth } from "./auth";
import { formatCredits } from "./credit-format";
import { bytes, GiB, type StorageAllowances, type StoragePackage } from "./storage-types";
import "./storage-packages.css";

const json = (body: unknown) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const date = (value: string) => new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });

export function StoragePanel({ workspaceId, admin = false, onBusyChange }: {
  workspaceId?: string; admin?: boolean; onBusyChange?: (busy: boolean) => void;
}) {
  const { session } = useAuth();
  const canPurchase = !admin && session.workspaces.find(w => w.id === session.workspace_id)?.role === "admin";
  const root = admin ? `/site/admin/workspaces/${workspaceId}/storage` : "/storage/allowances";
  const [data, setData] = useState<StorageAllowances | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [purchase, setPurchase] = useState<{ item: StoragePackage; id: string; version: number; expires: string } | null>(null);
  const [grant, setGrant] = useState(false);
  const [grantForm, setGrantForm] = useState({ storage: "0", download: "0", days: "30", reason: "" });
  const grantId = useRef("");
  const sequence = useRef(0);
  const [dialogError, setDialogError] = useState("");
  const load = useCallback(async () => {
    const current = ++sequence.current;
    setLoading(true); setError("");
    try { const result = await request<StorageAllowances>(root); if (sequence.current === current) setData(result); }
    catch (e) { if (sequence.current === current) setError(e instanceof Error ? e.message : "读取失败，请重试"); }
    finally { if (sequence.current === current) setLoading(false); }
  }, [root, session.workspace_id]);
  useEffect(() => { setData(null); setPurchase(null); setGrant(false); setNotice(""); void load(); return () => { sequence.current++; }; }, [load]);
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false); }, [busy, onBusyChange]);
  async function confirm() {
    if (!purchase || busy) return;
    setBusy(true); setDialogError("");
    try {
      await request("/storage/purchases", json({ package_id: purchase.item.id, version: purchase.version, request_id: purchase.id }));
      setPurchase(null); setNotice("额度已到账"); await load();
    } catch (e) { setDialogError(e instanceof Error ? e.message : "购买失败，请重试"); }
    finally { setBusy(false); }
  }
  return <section className="storage-allowances" aria-label="存储与下载额度">
    <div className="storage-toolbar"><span>{loading ? "正在读取" : ""}</span><button className="secondary" disabled={loading || busy} onClick={() => void load()}><RefreshCw size={16} />刷新</button></div>
    {error && <p className="auth-error" role="alert">{error}</p>}
    {notice && <p className="auth-success" role="status"><Check size={16} />{notice}</p>}
    {data && <>
      <dl className="storage-summary"><div><dt>存储空间</dt><dd>{bytes(data.used_bytes)} <small>/ {bytes(data.limit_bytes)}</small></dd></div>
        <div><dt>剩余下载额度</dt><dd>{bytes(data.download_remaining_bytes)}</dd></div></dl>
      <p className="storage-note">每月免费额度于 {date(data.download_resets_at)} 重置。</p>
      {data.download_usage && <p className="storage-note">本月用户下载 {bytes(data.download_usage.user_download)} · 模型读取 {bytes(data.download_usage.model_read)}</p>}
      {data.over_quota_since && <p className="auth-error" role="alert">存储空间已超出额度，暂停新增。请删除不需要的资源或增加额度；持续超额将于 {data.cleanup_due_at ? date(data.cleanup_due_at) : "宽限期结束"} 起清理最早存入的资源。</p>}
      {admin && <details className="storage-breakdown"><summary>用量明细</summary><dl>{Object.entries({ documents: "资料与保留版本", attachments: "资源附件", outputs: "资源文件", skills: "个人 Skill", reserved: "上传预留" }).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{bytes(data.breakdown[key] ?? 0)}</dd></div>)}</dl></details>}
      {!admin && data.packages.length > 0 && <section className="storage-package-section"><h2>增加额度</h2>
        <div className="storage-package-list">{data.packages.map(item => <div className="storage-package-row" key={item.id}>
          <div><strong>{item.name}</strong><span>{item.storage_bytes ? `${bytes(item.storage_bytes)} 存储 · ` : ""}{bytes(item.download_bytes)} 下载 · {item.days} 天</span></div>
          <div className="storage-package-action"><strong>{formatCredits(item.price_credits ?? 0)} 积分</strong>{canPurchase && <button id={`storage-buy-${item.id}`} className="secondary" disabled={busy} onClick={() => {
            setDialogError(""); setPurchase({ item, id: crypto.randomUUID(), version: data.config_version, expires: new Date(Date.now()+item.days*86400000).toISOString() });
          }}><Plus size={16} />购买</button>}</div>
        </div>)}</div>{!canPurchase && <p className="storage-note">请联系工作区管理员购买额度。</p>}</section>}
      {data.entitlements.length > 0 && <section><h2>可用套餐</h2><div className="storage-package-list">{data.entitlements.map(item => <div className="storage-package-row" key={item.id}>
        <div><strong>{item.name}</strong><span>{item.storage_bytes ? `${bytes(item.storage_bytes)} 存储 · ` : ""}剩余 {bytes(item.remaining_bytes)} 下载</span></div><span>{date(item.expires_at)} 到期</span>
      </div>)}</div></section>}
      {admin && <button className="secondary" onClick={() => { grantId.current = crypto.randomUUID(); setDialogError(""); setGrant(true); }}><Plus size={16} />增加额度</button>}
      {data.history.length > 0 && <details className="storage-breakdown"><summary>购买与调整记录</summary><div className="storage-package-list">{data.history.map(item => <div className="storage-package-row" key={item.id}>
        <div><strong>{item.name}</strong><span>{date(item.created_at)} · {date(item.expires_at)} 到期</span></div><span>{formatCredits(Number(item.price_credits))} 积分</span>
      </div>)}</div></details>}
    </>}
    {purchase && <Dialog title="确认购买" busy={busy} restoreFocusSelector={`#storage-buy-${purchase.item.id}`} onClose={() => { if (!busy) setPurchase(null); }}>
      <div className="storage-dialog-body">
      <dl className="storage-confirm"><div><dt>套餐</dt><dd>{purchase.item.name}</dd></div>
        <div><dt>扣除工作区积分</dt><dd>{formatCredits(purchase.item.price_credits ?? 0)} 积分</dd></div>
        <div><dt>增加存储空间</dt><dd>{bytes(purchase.item.storage_bytes)}</dd></div>
        <div><dt>增加下载额度</dt><dd>{bytes(purchase.item.download_bytes)}</dd></div>
        <div><dt>有效期</dt><dd>到账后 {purchase.item.days} 天，预计 {date(purchase.expires)} 到期</dd></div></dl>
        <p className="storage-note">一次购买，不会自动续费。</p>
      {dialogError && <p className="auth-error" role="alert">{dialogError}</p>}
      <div className="storage-dialog-actions"><button className="secondary" disabled={busy} onClick={() => setPurchase(null)}>取消</button><button className="primary" disabled={busy} onClick={() => void confirm()}>{busy ? <Loader2 size={16} className="spin" /> : <Check size={16} />}确认购买</button></div>
      </div>
    </Dialog>}
    {grant && <Dialog title="增加工作区额度" busy={busy} onClose={() => { if (!busy) setGrant(false); }}><form className="storage-grant-form storage-dialog-body" onSubmit={async e => {
      e.preventDefault(); if (busy) return; setBusy(true); setDialogError("");
      try { await request(`${root}/grants`, json({ storage_bytes: Number(grantForm.storage)*GiB, download_bytes: Number(grantForm.download)*GiB,
        days: Number(grantForm.days), reason: grantForm.reason, request_id: grantId.current })); setGrant(false); setNotice("额度已增加"); await load(); }
      catch (error) { setDialogError(error instanceof Error ? error.message : "保存失败"); }
      finally { setBusy(false); }
    }}><fieldset disabled={busy}>{([ ["storage", "存储空间（GiB）"], ["download", "下载额度（GiB）"], ["days", "有效天数"] ] as const).map(([key,label]) => <label key={key}>{label}<input type="number" min={key === "days" ? 1 : 0} max={key === "days" ? 365 : 1024} step="1" required value={grantForm[key]} onChange={e => { grantId.current=crypto.randomUUID(); setGrantForm({ ...grantForm, [key]: e.target.value }); }} /></label>)}
      <label>调整原因<input required maxLength={200} value={grantForm.reason} onChange={e => { grantId.current=crypto.randomUUID(); setGrantForm({ ...grantForm, reason:e.target.value }); }} /></label>
      {dialogError && <p className="auth-error" role="alert">{dialogError}</p>}<div className="storage-dialog-actions"><button type="button" className="secondary" onClick={() => setGrant(false)}>取消</button><button className="primary"><Check size={16} />确认增加</button></div></fieldset></form></Dialog>}
  </section>;
}
