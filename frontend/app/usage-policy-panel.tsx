"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Loader2, Save } from "lucide-react";
import { request } from "./api";

type Policy = {
  capability: string;
  unit: string;
  unit_size: number;
  credits_per_unit: number;
  enabled: boolean;
  version: number;
};
type Provider = {
  id: string;
  key: string;
  name: string;
  base_url: string;
  model: string;
  timeout_seconds: number;
  enabled: boolean;
  active: boolean;
  has_api_key: boolean;
};

const labels: Record<string, string> = {
  llm_input_tokens: "模型输入 Token",
  llm_output_tokens: "模型输出 Token",
  embedding_tokens: "Embedding Token",
  ocr_pages: "OCR 页数",
};

export function UsagePolicyPanel({ onBusyChange }: { onBusyChange: (busy: boolean) => void }) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [providerPricing, setProviderPricing] = useState<Record<string, Policy[]>>({});
  const [newProvider, setNewProvider] = useState({
    key: "", name: "", base_url: "", model: "", api_key: "",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [basePolicies, configuredProviders] = await Promise.all([
        request<Policy[]>("/usage/policies"),
        request<Provider[]>("/settings/llm-providers"),
      ]);
      setPolicies(basePolicies);
      setProviders(configuredProviders);
      const pricing = await Promise.all(configuredProviders.map(async (provider) => [
        provider.id, await request<Policy[]>(`/settings/llm-providers/${provider.id}/pricing`),
      ] as const));
      setProviderPricing(Object.fromEntries(pricing));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载规则失败");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function save(policy: Policy) {
    setSaving(policy.capability);
    onBusyChange(true);
    setError("");
    setNotice("");
    try {
      const updated = await request<Policy>(`/usage/policies/${policy.capability}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(policy),
      });
      setPolicies((items) => items.map((item) => item.capability === updated.capability ? updated : item));
      setNotice("计费规则已保存，新规则只影响后续用量");
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存规则失败");
    } finally {
      setSaving("");
      onBusyChange(false);
    }
  }

  async function createProvider(event: FormEvent) {
    event.preventDefault();
    setSaving("new");
    onBusyChange(true);
    try {
      const created = await request<Provider>("/settings/llm-providers", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(newProvider),
      });
      setProviders((items) => [...items, created]);
      setProviderPricing((items) => ({ ...items, [created.id]: [] }));
      setNewProvider({ key: "", name: "", base_url: "", model: "", api_key: "" });
      setNotice("模型供应商已添加");
    } catch (e) {
      setError(e instanceof Error ? e.message : "添加供应商失败");
    } finally {
      setSaving("");
      onBusyChange(false);
    }
  }

  async function activate(provider: Provider) {
    setSaving(provider.id);
    onBusyChange(true);
    try {
      const active = await request<Provider>(`/settings/llm-providers/${provider.id}/activate`, { method: "POST" });
      setProviders((items) => items.map((item) => ({ ...item, active: item.id === active.id })));
      setNotice(`已切换到 ${active.name}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "切换供应商失败");
    } finally {
      setSaving("");
      onBusyChange(false);
    }
  }

  async function saveProviderPrice(providerId: string, policy: Policy) {
    setSaving(`${providerId}:${policy.capability}`);
    onBusyChange(true);
    try {
      const updated = await request<Policy>(
        `/settings/llm-providers/${providerId}/pricing/${policy.capability}`,
        { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(policy) },
      );
      setProviderPricing((items) => ({
        ...items,
        [providerId]: (items[providerId] ?? []).map((item) =>
          item.capability === updated.capability ? updated : item),
      }));
      setNotice("供应商价格已保存，新价格只影响后续用量");
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存供应商价格失败");
    } finally {
      setSaving("");
      onBusyChange(false);
    }
  }

  return (
    <section className="billing-panel" aria-label="计费规则">
      <div className="billing-heading">
        <div>
          <span className="credit-label">内部计费规则</span>
          <small>仅管理员可见，用户端不会展示单价或公式</small>
        </div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="billing-notice" role="status">{notice}</p>}
      {loading ? <div className="billing-empty"><Loader2 size={18} className="spin" />正在加载</div> : (
        <div className="recharge-form">
          {policies.map((policy) => (
            <div className="recharge-option" key={policy.capability}>
              <span>
                {labels[policy.capability] ?? policy.capability}
                <small>版本 {policy.version}</small>
              </span>
              <input aria-label={`${policy.capability}计量单位`} value={policy.unit}
                onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, unit: e.target.value } : item))}
                style={{ width: 100 }} />
              <input aria-label={`${policy.capability}单位大小`} type="number" min={1}
                value={policy.unit_size} onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, unit_size: Number(e.target.value) } : item))}
                style={{ width: 90 }} />
              <input aria-label={`${policy.capability}积分`} type="number" min={0}
                value={policy.credits_per_unit} onChange={(e) => setPolicies((items) => items.map((item) =>
                  item.capability === policy.capability ? { ...item, credits_per_unit: Number(e.target.value) } : item))}
                style={{ width: 90 }} />
              <button className="icon-button" title="保存规则" aria-label="保存规则"
                disabled={saving === policy.capability} onClick={() => void save(policy)}>
                {saving === policy.capability ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
              </button>
            </div>
          ))}
        </div>
      )}
      <form className="recharge-form" onSubmit={(event) => void createProvider(event)}>
        <h3>添加对话模型供应商</h3>
        <div className="recharge-option">
          <input required placeholder="标识，如 qwen" value={newProvider.key}
            onChange={(e) => setNewProvider({ ...newProvider, key: e.target.value })} />
          <input required placeholder="名称" value={newProvider.name}
            onChange={(e) => setNewProvider({ ...newProvider, name: e.target.value })} />
          <input required placeholder="OpenAI 兼容 Base URL" value={newProvider.base_url}
            onChange={(e) => setNewProvider({ ...newProvider, base_url: e.target.value })} />
          <input required placeholder="模型名称" value={newProvider.model}
            onChange={(e) => setNewProvider({ ...newProvider, model: e.target.value })} />
          <input required type="password" placeholder="API Key" value={newProvider.api_key}
            onChange={(e) => setNewProvider({ ...newProvider, api_key: e.target.value })} />
          <button className="icon-button" title="添加供应商" aria-label="添加供应商" disabled={saving === "new"}>
            {saving === "new" ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
          </button>
        </div>
      </form>
      {providers.map((provider) => (
        <section className="billing-section" key={provider.id}>
          <div className="billing-heading">
            <div><strong>{provider.name}</strong><small>{provider.model} · {provider.key}</small></div>
            {provider.active ? <span>当前使用</span> : (
              <button className="text-button" disabled={saving === provider.id}
                onClick={() => void activate(provider)}>切换</button>
            )}
          </div>
          {(providerPricing[provider.id] ?? []).map((policy) => (
            <div className="recharge-option" key={policy.capability}>
              <span>{labels[policy.capability] ?? policy.capability}<small>版本 {policy.version}</small></span>
              <input type="number" min={1} value={policy.unit_size}
                aria-label="供应商价格单位大小"
                onChange={(e) => setProviderPricing((items) => ({
                  ...items, [provider.id]: items[provider.id].map((item) =>
                    item.capability === policy.capability ? { ...item, unit_size: Number(e.target.value) } : item),
                }))} style={{ width: 90 }} />
              <input type="number" min={0} value={policy.credits_per_unit}
                aria-label="供应商价格积分"
                onChange={(e) => setProviderPricing((items) => ({
                  ...items, [provider.id]: items[provider.id].map((item) =>
                    item.capability === policy.capability ? { ...item, credits_per_unit: Number(e.target.value) } : item),
                }))} style={{ width: 90 }} />
              <button className="icon-button" title="保存供应商价格" aria-label="保存供应商价格"
                disabled={saving === `${provider.id}:${policy.capability}`}
                onClick={() => void saveProviderPrice(provider.id, policy)}>
                {saving === `${provider.id}:${policy.capability}` ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
              </button>
            </div>
          ))}
        </section>
      ))}
    </section>
  );
}
