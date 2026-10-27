"use client";

import { CircleAlert, Loader2, Save } from "lucide-react";
import { Tooltip } from "../components/ui/tooltip";
import { policyLabels, type Policy } from "./model-types";

// One price-row layout for base-price editing and effective-price previews.
export function PolicyTable({ name, policies, busy, saving, update, save, isChanged, requirePrices = true, readOnly = false }: {
  name: string;
  policies: Policy[];
  busy: boolean;
  saving?: string;
  update?: (policy: Policy, patch: Partial<Policy>) => void;
  save?: (policy: Policy) => void;
  isChanged?: (policy: Policy) => boolean;
  requirePrices?: boolean;
  readOnly?: boolean;
}) {
  return <div className="policy-table" data-row-save={Boolean(save)} data-readonly={readOnly} role="table" aria-label={name}>
    <div className="policy-table-header" role="row"><span role="columnheader">能力</span><span role="columnheader">每单位用量</span><span role="columnheader">{readOnly ? "基准积分" : "消耗积分"}</span>{readOnly && <span role="columnheader">倍率后积分</span>}<span role="columnheader">规则状态</span>{save && <span role="columnheader" className="sr-only">操作</span>}</div>
    {policies.map(policy => {
      const label = policyLabels[policy.capability] ?? policy.capability;
      const changed = isChanged?.(policy) ?? false;
      const unit = policy.unit === "tokens" ? "Token" : policy.unit === "pages" ? "页" : policy.unit;
      const valid = policy.credits_per_unit !== null && Number.isInteger(policy.credits_per_unit) && policy.credits_per_unit >= 0 && Number.isInteger(policy.unit_size) && policy.unit_size > 0;
      if (readOnly) return <div className="policy-row" role="row" key={policy.capability}>
        <div className="policy-capability" role="cell"><strong>{label}</strong><small>{policy.source === "model_provider" ? "模型基准价" : policy.source === "platform_documents" ? "平台基准价" : "工作区旧规则"} · v{policy.version}</small></div>
        <div className="policy-field" role="cell"><span className="policy-field-label">每单位用量</span><span className="policy-value">{policy.unit_size.toLocaleString()} {unit}</span></div>
        <div className="policy-field" role="cell"><span className="policy-field-label">基准积分</span><span className="policy-value">{policy.configured === false ? "待配置" : policy.credits_per_unit}</span></div>
        <div className="policy-field" role="cell"><span className="policy-field-label">倍率后积分</span><strong className="policy-value">{policy.configured === false ? "—" : policy.effective_credits_per_unit !== undefined ? policy.effective_credits_per_unit ?? "—" : policy.credits_per_unit}</strong></div>
        <div className="policy-state" data-warning={policy.configured === false || !policy.enabled} role="cell">{policy.configured === false ? <><CircleAlert size={14} />待配置</> : policy.enabled ? "已配置" : "已停用"}</div>
      </div>;
      const fields = <>
        <div className="policy-capability" role="cell"><strong>{label}</strong><small data-missing={policy.configured === false}>
          {policy.configured === false ? <><CircleAlert size={13} aria-hidden="true" />待配置</> : `版本 ${policy.version}`}
          {policy.template_version ? ` · 来源模板 v${policy.template_version}` : ""}{changed && <span className="policy-unsaved">未保存</span>}</small>
        </div>
        <label className="policy-field" role="cell"><span className="policy-field-label">每单位用量</span><span className="policy-input"><input aria-label={`${name}${label}单位大小`} required type="number" min={1} max={2147483647} step={1} value={policy.unit_size} disabled={busy} onChange={event => update?.(policy, { unit_size: Number(event.target.value) })} /><span aria-hidden="true">{unit}</span></span></label>
        <label className="policy-field" role="cell"><span className="policy-field-label">消耗积分</span><span className="policy-input"><input aria-label={`${name}${label}积分`} required={requirePrices} type="number" min={0} max={2147483647} step={1} placeholder="待配置" value={policy.credits_per_unit ?? ""} disabled={busy} onChange={event => update?.(policy, { credits_per_unit: event.target.value === "" ? null : Number(event.target.value) })} /><span aria-hidden="true">积分</span></span></label>
        <div className="policy-state" role="cell"><label className="policy-toggle"><input type="checkbox" aria-label={`${name}${label}启用规则`} checked={policy.enabled} disabled={busy} onChange={event => update?.(policy, { enabled: event.target.checked })} />启用</label></div>
        {save && <div className="policy-action" role="cell"><Tooltip content={`保存${label}规则`}><button className="icon-button" aria-label={`保存${name}${label}规则`} disabled={busy || !valid || !changed}>{saving === policy.capability ? <Loader2 size={16} className="spin" /> : <Save size={16} />}</button></Tooltip></div>}
      </>;
      return save ? <form className="policy-row" role="row" key={policy.capability} onSubmit={event => { event.preventDefault(); save(policy); }}>{fields}</form>
        : <div className="policy-row" role="row" key={policy.capability}>{fields}</div>;
    })}
  </div>;
}
