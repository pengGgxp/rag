"use client";

import { useEffect, useState } from "react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useWorkspaceChanges } from "./admin/workspace-target";
import { policyPayload, type Policy, type ModelProvider } from "./model-types";
import { PolicyTable } from "./policy-table";
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
    const targets = [{ id: "base", name: "环境默认模型与文档处理", path: `${base}/usage-policies` },
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
      const updated = await request<Policy>(`${group.path}/${policy.capability}`, json(policyPayload(policy), "PUT"));
      const apply = (items: PriceGroup[]) => items.map(item => item.id !== group.id ? item : {
        ...item, policies: item.policies.map(row => row.capability === updated.capability ? updated : row) });
      setGroups(apply); setOriginal(apply);
      setNotice("计费规则已保存，仅影响后续消费");
    } catch (e) { setError(e instanceof Error ? e.message : "保存规则失败"); }
    finally { setSaving(""); }
  }
  return <section className="pricing-editor" aria-label="工作区计费规则">
    <p className="pricing-description">规则仅用于当前工作区，修改只影响后续消费。免费请明确填写 0。</p>
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <AdminLoad loading={loading || Boolean(providers && pricesLoading)} error={loadError || priceLoadError} retry={() => { if (loadError) void reload(); else setRetry(value => value + 1); }}>
      {groups.map(group => <section className="pricing-group" key={group.id}>
        <div className="pricing-group-heading"><h2>{group.name}</h2><span>{group.policies.filter(item => item.configured === false).length ? `${group.policies.filter(item => item.configured === false).length} 项待配置` : "价格已配置"}</span></div>
        <PolicyTable name={group.name} policies={group.policies} busy={Boolean(saving)}
          saving={saving.startsWith(`${group.id}:`) ? saving.slice(group.id.length + 1) : ""}
          isChanged={policy => JSON.stringify(policy) !== JSON.stringify(original.find(item => item.id === group.id)?.policies.find(item => item.capability === policy.capability))}
          update={(policy, patch) => update(group, policy, patch)} save={policy => void save(group, policy)} />
      </section>)}
    </AdminLoad>
  </section>;
}
