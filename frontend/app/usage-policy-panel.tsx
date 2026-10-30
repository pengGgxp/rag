"use client";

import { useEffect, useState } from "react";
import { CircleAlert, Loader2, Save } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useWorkspaceChanges } from "./admin/workspace-target";
import { PolicyTable } from "./policy-table";
import { availabilityLabel, type Policy } from "./model-types";
import "./billing.css";

type BillingSettings = {
  version: number; multiplier: string; shared_pricing_enabled: boolean; billing_enabled: boolean;
  document_policies: Policy[];
  search_policies?: Policy[];
  models: { provider_id: string; name: string; model: string; available: boolean; unavailable_reason: string | null; policies: Policy[] }[];
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
      : (numerator / 100).toFixed(6).replace(/\.?0+$/, "") };
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
  const [showUnavailable, setShowUnavailable] = useState(false);
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
          <span className="billing-muted" id="multiplier-note">统一作用于模型、文档处理与联网工具 · 版本 {saved.version}{dirty ? " · 未保存" : ""}</span>
        </form>
        <section className="pricing-group">
          <div className="pricing-group-heading"><h2>模型价格 <small className="billing-muted">{dirty ? "未保存预览" : "当前价格预览"}</small></h2>
            <label className="policy-toggle"><input type="checkbox" checked={showUnavailable} onChange={e => setShowUnavailable(e.target.checked)} />显示不可用模型</label>
          </div>
          <p className="pricing-description">倍率后价格为每单位用量的价格，不代表单次请求扣分。实际扣分按当前结算规则计算。</p>
          {!saved.billing_enabled && <p className="pricing-cache-note">缓存读取单价仅作配置预览；当前结算尚未启用缓存单独计价。</p>}
          <div className="model-price-table" role="table" aria-label="模型价格对照">
            <div className="model-price-heading" role="row"><span role="columnheader">模型 / 状态</span><span role="columnheader">输入</span><span role="columnheader">缓存读取</span><span role="columnheader">输出</span></div>
            {saved.models.filter(model => showUnavailable || model.available).map(model => <div className="model-price-row" role="row" key={model.provider_id}>
              <div className="model-price-identity" role="cell"><strong>{model.name}</strong><small>{model.model}</small><span className="model-availability" data-warning={!model.available}>{availabilityLabel(model.unavailable_reason)}</span></div>
              {["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"].map((capability, index) => {
                const price = effective(model.policies, percent).find(p => p.capability === capability);
                return <div className="model-price-cell" role="cell" key={capability}><span className="model-price-label">{["输入", "缓存读取", "输出"][index]}</span>
                  {price && price.configured !== false ? <><strong>{price.effective_credits_per_unit ?? "—"} <small>积分</small></strong><small>基准 {price.credits_per_unit} 积分</small><small>/ {price.unit_size.toLocaleString()} Token{!price.enabled ? " · 已停用" : ""}</small></> : <span className="model-availability" data-warning>待配置</span>}
                </div>;
              })}
            </div>)}
          </div>
          {!saved.models.some(model => showUnavailable || model.available) && <p className="billing-empty">{saved.models.length ? "暂无可用模型，可勾选查看不可用原因" : "当前工作区暂无可见模型"}</p>}
        </section>
        <section className="pricing-group"><div className="pricing-group-heading"><h2>文档处理</h2><span>{dirty ? "未保存预览" : "平台基准价 × 工作区倍率"}</span></div>
          <PolicyTable name="文档处理价格预览" readOnly policies={effective(saved.document_policies, percent)} busy={busy} />
        </section>
        {!!saved.search_policies?.length && <section className="pricing-group"><div className="pricing-group-heading"><h2>联网工具</h2><span>{dirty ? "未保存预览" : "平台基准价 × 工作区倍率"}</span></div>
          <PolicyTable name="联网工具价格预览" readOnly policies={effective(saved.search_policies, percent)} busy={busy} />
        </section>}
      </>}
    </AdminLoad>
  </section>;
}
