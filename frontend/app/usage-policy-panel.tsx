"use client";

import { useEffect, useState } from "react";
import { CircleAlert, Loader2, Save } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useWorkspaceChanges } from "./admin/workspace-target";
import { PolicyTable } from "./policy-table";
import type { Policy } from "./model-types";
import "./billing.css";

type BillingSettings = {
  version: number; multiplier: string; shared_pricing_enabled: boolean; billing_enabled: boolean;
  document_policies: Policy[];
  models: { binding_id: string; name: string; model: string; active: boolean; policies: Policy[] }[];
};

function ratePercent(value: string) {
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const percent = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return percent >= 1 && percent <= 10000 ? percent : null;
}

function effective(policies: Policy[], percent: number | null): Policy[] {
  return policies.map(policy => {
    const numerator = policy.credits_per_unit !== null && percent !== null ? policy.credits_per_unit * percent : null;
    return { ...policy, effective_credits_per_unit: numerator === null ? null
      : `${Math.floor(numerator / 100)}.${String(numerator % 100).padStart(2, "0")}` };
  });
}

export function UsagePolicyPanel({ workspaceId }: { workspaceId: string }) {
  const endpoint = `/site/admin/workspaces/${encodeURIComponent(workspaceId)}/billing-settings`;
  const { data, loading, error: loadError, reload } = useAdminResource<BillingSettings>(endpoint);
  const [multiplier, setMultiplier] = useState("");
  const [saved, setSaved] = useState<BillingSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { if (data) { setSaved(data); setMultiplier(data.multiplier); } }, [data]);
  const percent = ratePercent(multiplier);
  const dirty = Boolean(saved && (percent === null || percent !== ratePercent(saved.multiplier)));
  useWorkspaceChanges(dirty, busy);

  async function save() {
    if (!saved || percent === null) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await request<BillingSettings>(endpoint, json({ version: saved.version, multiplier }, "PUT"));
      setSaved(updated); setMultiplier(updated.multiplier);
      setNotice(updated.shared_pricing_enabled ? "消费倍率已保存，后续新请求生效" : "倍率已保存，共享计费启用后生效");
    } catch (e) { setError(e instanceof Error ? e.message : "保存倍率失败，请重试"); }
    finally { setBusy(false); }
  }

  return <section className="pricing-editor" aria-label="工作区消费倍率">
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {error && <div className="admin-error" role="alert"><span>{error}</span><button className="secondary" disabled={busy} onClick={() => { setError(""); void reload(); }}>重新加载并放弃修改</button></div>}
    <AdminLoad loading={loading} error={loadError} retry={reload}>
      {saved && <>
        {!saved.shared_pricing_enabled && <div className="billing-pending"><CircleAlert size={18} /><div><strong>共享计费待启用</strong><p>当前消费仍使用旧规则，以下为待启用价格。</p></div></div>}
        <form className="multiplier-form" onSubmit={event => { event.preventDefault(); void save(); }}>
          <label htmlFor="workspace-multiplier">消费倍率<span className="policy-input"><input id="workspace-multiplier" aria-describedby="multiplier-note" type="number" required min="0.01" max="100" step="0.01" inputMode="decimal" value={multiplier} disabled={busy} onChange={e => setMultiplier(e.target.value)} /><span>×</span></span></label>
          <button className="primary" disabled={busy || !dirty || percent === null}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存倍率</button>
          <span className="billing-muted" id="multiplier-note">模型、OCR 与 Embedding 共用 · 版本 {saved.version}{dirty ? " · 未保存" : ""}</span>
        </form>
        <section className="pricing-group"><div className="pricing-group-heading"><h2>文档处理</h2><span>{dirty ? "未保存预览" : "平台基准价 × 工作区倍率"}</span></div>
          <PolicyTable name="文档处理价格预览" readOnly policies={effective(saved.document_policies, percent)} busy={busy} />
        </section>
        {saved.models.map(model => <section className="pricing-group" key={model.binding_id}>
          <div className="pricing-group-heading"><h2>{model.name}<small className="billing-muted"> · {model.model}</small></h2><span>{model.active ? "当前模型" : "已关联模型"}</span></div>
          <PolicyTable name={`${model.name}价格预览`} readOnly policies={effective(model.policies, percent)} busy={busy} />
        </section>)}
        {!saved.models.length && <p className="billing-empty">尚未关联模型</p>}
      </>}
    </AdminLoad>
  </section>;
}
