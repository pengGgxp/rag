"use client";

import { useEffect, useState } from "react";
import { Loader2, Save } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useWorkspaceChanges } from "./admin/workspace-target";
import { policyLabels, type Policy, type ModelProvider } from "./model-types";
import "./billing.css";

type PriceGroup = { id: string; name: string; path: string; policies: Policy[] };

export function UsagePolicyPanel({ workspaceId }: { workspaceId: string }) {
  const base = `/site/admin/workspaces/${encodeURIComponent(workspaceId)}`;
  const [groups, setGroups] = useState<PriceGroup[]>([]);
  const [original, setOriginal] = useState<PriceGroup[]>([]);
  const [error, setError] = useState("");
  const [priceLoadError, setPriceLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState("");
  const { data: providers, loading, error: loadError, reload } = useAdminResource<ModelProvider[]>(`${base}/llm-providers`);
  const [pricesLoading, setPricesLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useWorkspaceChanges(JSON.stringify(groups) !== JSON.stringify(original), Boolean(saving));
  useEffect(() => {
    if (!providers) return;
    let active = true;
    setPricesLoading(true); setPriceLoadError("");
    const targets = [{ id: "base", name: "基础计费规则", path: `${base}/usage-policies` },
      ...providers.map(item => ({ id: item.id, name: `${item.name} · ${item.model}`, path: `${base}/llm-providers/${item.id}/pricing` }))];
    Promise.all(targets.map(async item => ({ ...item, policies: await request<Policy[]>(item.path) })))
      .then(items => { if (active) { setGroups(items); setOriginal(items); } })
      .catch(e => { if (active) setPriceLoadError(e instanceof Error ? e.message : "加载价格失败"); })
      .finally(() => { if (active) setPricesLoading(false); });
    return () => { active = false; };
  }, [providers, base, retry]);

  function update(group: PriceGroup, policy: Policy, patch: Partial<Policy>) {
    setGroups(items => items.map(item => item.id !== group.id ? item : { ...item,
      policies: item.policies.map(row => row.capability === policy.capability ? { ...row, ...patch } : row) }));
  }
  async function save(group: PriceGroup, policy: Policy) {
    setSaving(`${group.id}:${policy.capability}`); setError(""); setNotice("");
    try {
      const { version: _version, ...payload } = policy;
      const updated = await request<Policy>(`${group.path}/${policy.capability}`, json(payload, "PUT"));
      const apply = (items: PriceGroup[]) => items.map(item => item.id !== group.id ? item : {
        ...item, policies: item.policies.map(row => row.capability === updated.capability ? updated : row) });
      setGroups(apply); setOriginal(apply);
      setNotice("计费规则已保存，仅影响后续消费");
    } catch (e) { setError(e instanceof Error ? e.message : "保存规则失败"); }
    finally { setSaving(""); }
  }
  return <section aria-label="工作区计费规则">
    <p className="billing-muted">规则仅用于当前工作区，历史账单保持原有价格快照。</p>
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <AdminLoad loading={loading || Boolean(providers && pricesLoading)} error={loadError || priceLoadError} retry={() => { if (loadError) void reload(); else setRetry(value => value + 1); }}>
      {groups.map(group => <section className="admin-section" key={group.id}>
        <h2>{group.name}</h2>
        <div className="policy-table" role="table" aria-label={group.name}>
          <div className="policy-table-header" role="row"><span role="columnheader">能力</span><span role="columnheader">计量单位</span><span role="columnheader">每单位大小</span><span role="columnheader">消耗积分</span><span role="columnheader">操作</span></div>
          {group.policies.map(policy => <form className="policy-row" role="row" key={policy.capability} onSubmit={event => { event.preventDefault(); void save(group, policy); }}>
            <div className="policy-capability" role="cell"><strong>{policyLabels[policy.capability] ?? policy.capability}</strong><small>版本 {policy.version}</small>
              <label className="policy-toggle"><input type="checkbox" checked={policy.enabled} disabled={Boolean(saving)} onChange={event => update(group, policy, { enabled: event.target.checked })} />启用计费</label>
            </div>
            <label className="policy-field" role="cell"><span className="sr-only">{`${group.name}${policy.capability}计量单位`}</span><input required maxLength={40} value={policy.unit} disabled={Boolean(saving)} onChange={event => update(group, policy, { unit: event.target.value })} /></label>
            <label className="policy-field" role="cell"><span className="sr-only">{`${group.name}${policy.capability}单位大小`}</span><input required type="number" min={1} step={1} value={policy.unit_size} disabled={Boolean(saving)} onChange={event => update(group, policy, { unit_size: Number(event.target.value) })} /></label>
            <label className="policy-field" role="cell"><span className="sr-only">{`${group.name}${policy.capability}积分`}</span><input required type="number" min={0} step={1} value={policy.credits_per_unit} disabled={Boolean(saving)} onChange={event => update(group, policy, { credits_per_unit: Number(event.target.value) })} /></label>
            <div className="policy-action" role="cell"><button className="icon-button" title="保存规则" aria-label={`保存${group.name}${policyLabels[policy.capability]}规则`} disabled={Boolean(saving)}>{saving === `${group.id}:${policy.capability}` ? <Loader2 size={16} className="spin" /> : <Save size={16} />}</button></div>
          </form>)}
        </div>
      </section>)}
    </AdminLoad>
  </section>;
}
