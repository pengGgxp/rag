"use client";

import { Loader2, Save } from "lucide-react";
import { policyLabels, type Policy } from "./model-types";

// Shared by workspace prices and the atomic new-workspace template form.
export function PolicyTable({ name, policies, busy, saving, update, save }: {
  name: string;
  policies: Policy[];
  busy: boolean;
  saving?: string;
  update: (policy: Policy, patch: Partial<Policy>) => void;
  save?: (policy: Policy) => void;
}) {
  return <div className="policy-table" role="table" aria-label={name}>
    <div className="policy-table-header" role="row"><span role="columnheader">能力</span><span role="columnheader">计量单位</span><span role="columnheader">每单位大小</span><span role="columnheader">消耗积分</span><span role="columnheader">{save ? "操作" : ""}</span></div>
    {policies.map(policy => {
      const label = policyLabels[policy.capability] ?? policy.capability;
      const fields = <>
        <div className="policy-capability" role="cell"><strong>{label}</strong><small>{policy.configured === false ? "待配置" : `版本 ${policy.version}`}{policy.template_version ? ` · 来源模板 v${policy.template_version}` : ""}</small>
          <label className="policy-toggle"><input type="checkbox" checked={policy.enabled} disabled={busy} onChange={event => update(policy, { enabled: event.target.checked })} />启用计费</label>
        </div>
        <label className="policy-field" role="cell"><span className="sr-only">{`${name}${label}计量单位`}</span><input aria-label={`${name}${label}计量单位`} required maxLength={40} value={policy.unit} disabled={busy} onChange={event => update(policy, { unit: event.target.value })} /></label>
        <label className="policy-field" role="cell"><span className="sr-only">{`${name}${label}单位大小`}</span><input aria-label={`${name}${label}单位大小`} required type="number" min={1} max={2147483647} step={1} value={policy.unit_size} disabled={busy} onChange={event => update(policy, { unit_size: Number(event.target.value) })} /></label>
        <label className="policy-field" role="cell"><span className="sr-only">{`${name}${label}积分`}</span><input aria-label={`${name}${label}积分`} required type="number" min={0} max={2147483647} step={1} placeholder="待配置" value={policy.credits_per_unit ?? ""} disabled={busy} onChange={event => update(policy, { credits_per_unit: event.target.value === "" ? null : Number(event.target.value) })} /></label>
        <div className="policy-action" role="cell">{save && <button className="icon-button" title={`保存${label}规则`} aria-label={`保存${name}${label}规则`} disabled={busy}>{saving === policy.capability ? <Loader2 size={16} className="spin" /> : <Save size={16} />}</button>}</div>
      </>;
      return save ? <form className="policy-row" role="row" key={policy.capability} onSubmit={event => { event.preventDefault(); save(policy); }}>{fields}</form>
        : <div className="policy-row" role="row" key={policy.capability}>{fields}</div>;
    })}
  </div>;
}
