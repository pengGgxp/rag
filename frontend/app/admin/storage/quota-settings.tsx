"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Calculator, Check, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { ApiError, request } from "../../api";
import { PricingCalculator } from "../../pricing-calculator";
import { GiB, MiB, bytes, type StorageQuota } from "../../storage-types";
import { AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import "../../storage-packages.css";

export function QuotaSettings({ onState }: { onState: (state: { dirty: boolean; busy: boolean }) => void }) {
  const resource = useAdminResource<StorageQuota>("/site/admin/storage/quotas");
  const [form, setForm] = useState<StorageQuota | null>(null);
  const [original, setOriginal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [calculator, setCalculator] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const workspaces = useAdminResource<{ items: { id: string; name: string; used_bytes: number; limit_bytes: number; over_quota_since: string | null; cleanup_due_at: string | null }[]; has_more: boolean }>(`/site/admin/storage/workspaces?page=${page}`);
  const dirty = Boolean(form && JSON.stringify(form) !== original);
  useAdminChanges(dirty, busy);
  useEffect(() => { onState({ dirty, busy }); }, [dirty, busy, onState]);
  useEffect(() => { if (resource.data) { setForm(resource.data); setOriginal(JSON.stringify(resource.data)); } }, [resource.data]);
  function patchPackage(index: number, patch: Partial<StorageQuota["packages"][number]>) {
    if (form) setForm({ ...form, packages: form.packages.map((p, i) => i === index ? { ...p, ...patch } : p) });
  }
  async function save() {
    if (!form || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<StorageQuota>("/site/admin/storage/quotas", json(form, "PUT"));
      setForm(value); setOriginal(JSON.stringify(value)); setNotice("已保存"); setConflict(false);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); setConflict(e instanceof ApiError && e.status === 409); }
    finally { setBusy(false); }
  }
  return <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>{form && <>
    {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
    {error && <p className="admin-error" role="alert">{error}</p>}
    {conflict && <div className="admin-inline-confirm"><span>重新加载会放弃本页修改。</span><button className="secondary" disabled={busy} onClick={() => { setConflict(false); void resource.reload(); }}>放弃修改并重新加载</button></div>}
    <form className="provider-form model-detail-form" onSubmit={e => { e.preventDefault(); void save(); }}>
      <fieldset className="provider-form-fields" disabled={busy}>
        <section className="model-form-section"><h2>免费额度</h2><div className="provider-form-grid">
          {([ ["free_storage_bytes", "免费存储空间（MiB）"], ["free_download_bytes", "每月免费下载（MiB）"] ] as const).map(([key, label]) => <label key={key}>{label}<input required type="number" min="0" max="1048576" step="1" value={form[key]/MiB} onChange={e => setForm({ ...form, [key]: Number(e.target.value)*MiB })} /></label>)}
        </div></section>
        <section className="model-form-section"><h2>套餐</h2>
          {form.packages.map((p,index) => <details className="quota-package" key={p.id} open={index === 0}>
            <summary><strong>{p.name || "新套餐"}</strong><span>{p.price_credits === null ? "未定价" : `${p.price_credits} 积分`} · {p.enabled ? "启用" : "停用"}</span></summary>
            <header><h3>编辑套餐</h3><button type="button" className="icon-button" aria-label={`移除${p.name}`} title="移除套餐" onClick={() => setForm({ ...form, packages:form.packages.filter((_,i) => i !== index) })}><Trash2 size={16} /></button></header>
            <div className="provider-form-grid"><label>名称<input required maxLength={80} value={p.name} onChange={e => patchPackage(index,{name:e.target.value})} /></label>
              <label>积分售价<input required={p.enabled} type="number" min="0" max="2147483647" step="0.0001" placeholder="未配置" value={p.price_credits ?? ""} onChange={e => patchPackage(index,{price_credits:e.target.value === "" ? null : Number(e.target.value)})} /></label>
              <label>存储空间（GiB）<input required type="number" min="0" max="1024" step="1" value={p.storage_bytes/GiB} onChange={e => patchPackage(index,{storage_bytes:Number(e.target.value)*GiB})} /></label>
              <label>下载额度（GiB）<input required type="number" min="0" max="1024" step="1" value={p.download_bytes/GiB} onChange={e => patchPackage(index,{download_bytes:Number(e.target.value)*GiB})} /></label>
              <label>有效天数<input required type="number" min="1" max="365" value={p.days} onChange={e => patchPackage(index,{days:Number(e.target.value)})} /></label>
              <label>排序<input required type="number" min="0" max="1000" value={p.order} onChange={e => patchPackage(index,{order:Number(e.target.value)})} /></label>
              <label className="provider-enabled"><input type="checkbox" checked={p.enabled} onChange={e => patchPackage(index,{enabled:e.target.checked})} />启用套餐</label>
              <button type="button" id={`storage-calculate-${index}`} className="secondary" onClick={() => setCalculator(index)}><Calculator size={16} />按成本计算</button>
            </div></details>)}
          <button type="button" className="secondary" disabled={form.packages.length >= 30} onClick={() => setForm({ ...form, packages:[...form.packages, { id:`package-${crypto.randomUUID().slice(0,8)}`, name:"", storage_bytes:GiB, download_bytes:GiB, days:30, price_credits:null, enabled:false, order:form.packages.length }] })}><Plus size={16} />添加套餐</button>
        </section>
        <section className="model-form-section"><details><summary>成本参考</summary><div className="provider-form-grid">
          {Object.entries({ storage_yuan_gib_month:"标准存储（元 / GiB / 月）", egress_yuan_gib:"公网流量（元 / GiB）", reads_yuan_10000:"读取请求（元 / 万次）", writes_yuan_10000:"写入请求（元 / 万次）", transfer_yuan_gib:"平台传输分摊（元 / GiB）" }).map(([key,label]) => <label key={key}>{label}<input inputMode="decimal" pattern="[0-9]+(\.[0-9]{1,8})?" required value={form.costs[key] ?? "0"} onChange={e => setForm({ ...form, costs:{...form.costs,[key]:e.target.value} })} /></label>)}
        </div><p className="storage-note">成本参考不自动修改已发布售价。</p></details></section>
        <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span><button className="primary" disabled={busy || !dirty}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存额度与套餐</button></div>
      </fieldset>
    </form>
    {calculator !== null && form.packages[calculator] && <PricingCalculator busy={busy} close={() => setCalculator(null)} restoreFocusSelector={`#storage-calculate-${calculator}`}
      policy={{ capability:"storage_package", unit:"packages", unit_size:1, credits_per_unit:form.packages[calculator].price_credits, enabled:true, version:0 }}
      initialCost={String(Number(form.costs.storage_yuan_gib_month ?? 0)*form.packages[calculator].storage_bytes/GiB*form.packages[calculator].days/30 + (Number(form.costs.egress_yuan_gib ?? 0)+Number(form.costs.transfer_yuan_gib ?? 0))*form.packages[calculator].download_bytes/GiB)}
      apply={patch => patchPackage(calculator,{price_credits:patch.credits_per_unit ?? null})} />}
    <section className="admin-section"><h2>工作区用量</h2><AdminLoad loading={workspaces.loading} error={workspaces.error} retry={workspaces.reload}>
      <div className="admin-table-wrap"><table className="admin-table storage-workspace-table"><thead><tr><th>工作区</th><th>已用 / 额度</th><th>状态</th></tr></thead><tbody>{workspaces.data?.items.map(w => <tr key={w.id}><td><Link href={`/admin/workspaces/${w.id}`}>{w.name}</Link></td><td>{bytes(w.used_bytes)} / {bytes(w.limit_bytes)}</td><td>{w.over_quota_since ? w.cleanup_due_at && new Date(w.cleanup_due_at) < new Date() ? "超额待处理" : "需增加额度" : "正常"}</td></tr>)}</tbody></table></div>
      <div className="storage-dialog-actions"><button className="secondary" disabled={page === 1 || workspaces.loading} onClick={() => setPage(p=>p-1)}>上一页</button><span>{page}</span><button className="secondary" disabled={!workspaces.data?.has_more || workspaces.loading} onClick={() => setPage(p=>p+1)}>下一页</button></div>
    </AdminLoad></section>
  </>}</AdminLoad>;
}
