"use client";

import { useEffect, useState } from "react";
import { Check, Plus, Save, Trash2 } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminLoad, json, useAdminResource } from "../admin-ui";

type Package = { id: string; name: string; amount_fen: number; credits: number };
type Method = { id: string; label: string; provider_key: string; code: string; enabled: boolean };
type Config = { version: number; source: string; packages: Package[]; custom_recharge_enabled: boolean;
  custom_credits_per_yuan: number; payment_methods: Method[] };
const endpoint = "/site/admin/payments/recharge";

export function RechargeSettings({ onStateChange }: { onStateChange: (dirty: boolean, busy: boolean) => void }) {
  const resource = useAdminResource<Config>(endpoint);
  const [form, setForm] = useState<Config | null>(null);
  const [original, setOriginal] = useState<Config | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  useEffect(() => { if (resource.data) { setForm(resource.data); setOriginal(resource.data); } }, [resource.data]);
  useEffect(() => { onStateChange(dirty, busy); }, [dirty, busy, onStateChange]);
  useEffect(() => () => onStateChange(false, false), [onStateChange]);

  async function save() {
    if (!form) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const { source: _, ...payload } = form;
      const saved = await request<Config>(endpoint, json(payload, "PUT"));
      setForm(saved); setOriginal(saved); setNotice("充值设置已保存"); setConflict(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally { setBusy(false); }
  }
  function patchPackage(index: number, patch: Partial<Package>) {
    if (form) setForm({ ...form, packages: form.packages.map((p, i) => i === index ? { ...p, ...patch } : p) });
  }
  function patchMethod(index: number, patch: Partial<Method>) {
    if (form) setForm({ ...form, payment_methods: form.payment_methods.map((m, i) => i === index ? { ...m, ...patch } : m) });
  }
  return <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
    {form && <form className="provider-form model-detail-form payment-settings-form" onSubmit={e => { e.preventDefault(); void save(); }}>
      {error && <p className="admin-error" role="alert">{error}</p>}
      {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
      {conflict && <p className="admin-inline-confirm"><span>重新加载会放弃当前修改。</span><button type="button" className="secondary" onClick={() => { setConflict(false); setError(""); void resource.reload(); }}>放弃修改并重新加载</button></p>}
      <fieldset className="provider-form-fields" disabled={busy}>
        <section className="model-form-section">
          <div className="payment-packages-heading"><h2>充值档位</h2><button type="button" className="secondary" disabled={form.packages.length >= 20}
            onClick={() => setForm({ ...form, packages: [...form.packages, { id: `package_${crypto.randomUUID().slice(0, 12)}`, name: "", amount_fen: 0, credits: 0 }] })}><Plus size={16} />添加档位</button></div>
          <div className="payment-package-list">{form.packages.map((p, i) => <div className="payment-package-row" key={i}>
            <label>档位 ID<input required value={p.id} maxLength={64} onChange={e => patchPackage(i, { id: e.target.value })} /></label>
            <label>显示名称<input required value={p.name} maxLength={100} onChange={e => patchPackage(i, { name: e.target.value })} /></label>
            <label>金额（元）<input required type="number" min="0.01" max="10000" step="0.01" value={p.amount_fen ? p.amount_fen / 100 : ""} onChange={e => patchPackage(i, { amount_fen: Math.round(Number(e.target.value) * 100) })} /></label>
            <label>到账积分<input required type="number" min="1" max="1000000" step="1" value={p.credits || ""} onChange={e => patchPackage(i, { credits: Number(e.target.value) })} /></label>
            <button type="button" className="icon-button payment-package-remove" aria-label={`移除${p.name || "此"}档位`} title="移除档位" onClick={() => setForm({ ...form, packages: form.packages.filter((_, n) => n !== i) })}><Trash2 size={16} /></button>
          </div>)}</div>
          <label className="provider-enabled payment-custom-recharge-toggle"><input type="checkbox" checked={form.custom_recharge_enabled} onChange={e => setForm({ ...form, custom_recharge_enabled: e.target.checked })} />允许用户自定义充值金额</label>
          {form.custom_recharge_enabled && <label>自定义充值比例（积分 / 元）<input required type="number" min="1" max="1000000" step="1" value={form.custom_credits_per_yuan} onChange={e => setForm({ ...form, custom_credits_per_yuan: Number(e.target.value) })} /></label>}
        </section>
        <section className="model-form-section">
          <div className="payment-packages-heading"><h2>收银台付款方式</h2><button type="button" className="secondary" disabled={form.payment_methods.length >= 40}
            onClick={() => setForm({ ...form, payment_methods: [...form.payment_methods, { id: crypto.randomUUID(), label: "", provider_key: "ezfp", code: "", enabled: false }] })}><Plus size={16} />添加方式</button></div>
          <div className="payment-package-list">{form.payment_methods.map((m, i) => <div className="payment-package-row payment-routing-row" key={m.id}>
            <label>显示名称<input required maxLength={60} value={m.label} onChange={e => patchMethod(i, { label: e.target.value })} placeholder="例如 微信支付" /></label>
            <label>支付渠道<select value={m.provider_key} onChange={e => patchMethod(i, { provider_key: e.target.value })}><option value="ezfp">易支付</option><option value="zhifux">支付 FM</option></select></label>
            <label>网关编码<input required maxLength={100} value={m.code} onChange={e => patchMethod(i, { code: e.target.value })} placeholder="例如 wxpay" /></label>
            <label className="provider-enabled"><input type="checkbox" checked={m.enabled} onChange={e => patchMethod(i, { enabled: e.target.checked })} />启用</label>
            <button type="button" className="icon-button payment-package-remove" title="移除付款方式" aria-label={`移除${m.label || "此"}付款方式`} onClick={() => setForm({ ...form, payment_methods: form.payment_methods.filter((_, n) => n !== i) })}><Trash2 size={16} /></button>
          </div>)}</div>
        </section>
        <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span><button className="primary" disabled={busy || !dirty || conflict}><Save size={16} />{busy ? "保存中" : "保存充值设置"}</button></div>
      </fieldset>
    </form>}
  </AdminLoad>;
}
