"use client";

import { useState } from "react";
import { Loader2, Pencil, Plus, Save } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useAdminChanges } from "./admin/admin-changes";
import type { ModelProvider, Policy } from "./model-types";
import { PolicyTable } from "./policy-table";
import "./billing.css";

const blank = { key: "", name: "", base_url: "", model: "", api_key: "", timeout_seconds: 60, enabled: false, published: true, version: 0 };
type ProviderForm = typeof blank;

export function ModelProviderPanel() {
  const base = "/site/admin/llm-providers";
  const { data: providers, loading, error: loadError, reload } = useAdminResource<ModelProvider[]>(base);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderForm>(blank);
  const [original, setOriginal] = useState<ProviderForm>(blank);
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pricing, setPricing] = useState<ModelProvider | null>(null);
  const [prices, setPrices] = useState<Policy[]>([]);
  const [originalPrices, setOriginalPrices] = useState<Policy[]>([]);
  const dirty = (editing !== null && JSON.stringify(form) !== JSON.stringify(original)) || JSON.stringify(prices) !== JSON.stringify(originalPrices);
  useAdminChanges(dirty, busy);
  function edit(provider?: ModelProvider) {
    const value = provider ? { key: provider.key, name: provider.name, base_url: provider.base_url, model: provider.model, api_key: "", timeout_seconds: provider.timeout_seconds, enabled: provider.enabled, published: provider.published, version: provider.version } : blank;
    setEditing(provider?.id ?? "new"); setForm(value); setOriginal(value); setError(""); setNotice("");
  }
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await run(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); }
    finally { setBusy(false); }
  }
  async function save() {
    const { api_key, ...fields } = form;
    await request<ModelProvider>(editing === "new" ? base : `${base}/${editing}`, json({ ...fields, ...(api_key ? { api_key } : {}) }, editing === "new" ? "POST" : "PUT"));
    setForm(blank); setOriginal(blank); setEditing(null);
    setNotice("模型供应商已保存"); await reload();
  }
  function editPrices(provider: ModelProvider) {
    const values = ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"].map(capability => ({
      capability, unit: "tokens", unit_size: 1000, credits_per_unit: null, enabled: true, version: provider.version,
      ...provider.prices.find(p => p.capability === capability),
      configured: provider.prices.some(p => p.capability === capability),
    }));
    setPricing(provider); setPrices(values); setOriginalPrices(values); setError(""); setNotice("");
  }
  async function reloadEditor() {
    const latest = await request<ModelProvider[]>(base);
    const current = latest.find(p => p.id === (editing ?? pricing?.id));
    if (editing && current) edit(current);
    else if (pricing && current) editPrices(current);
    else { setEditing(null); setPricing(null); setPrices([]); setOriginalPrices([]); }
    await reload();
  }
  const pricesComplete = prices.every(p => Number.isInteger(p.credits_per_unit) && p.credits_per_unit !== null && p.credits_per_unit >= 0 && Number.isInteger(p.unit_size) && p.unit_size > 0 && p.enabled);
  return <section aria-label="站点模型供应商">
    {error && <div className="admin-error" role="alert"><span>{error}</span><button className="secondary" disabled={busy} onClick={() => void action(reloadEditor)}>重新加载并放弃修改</button></div>}
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    <AdminLoad loading={loading} error={loadError} retry={reload}>
      {providers && <>
        {!providers.length && <p className="billing-muted">尚未配置模型供应商</p>}
        {editing === null && !pricing && <button className="secondary" onClick={() => edit()}><Plus size={16} />添加供应商</button>}
        {pricing && <form className="pricing-editor" onSubmit={e => { e.preventDefault(); void action(async () => {
          await request(`${base}/${pricing.id}/pricing`, json({ version: pricing.version, policies: prices.map(({ capability, unit, unit_size, credits_per_unit, enabled }) => ({ capability, unit, unit_size, credits_per_unit, enabled })) }, "PUT"));
          setPricing(null); setPrices([]); setOriginalPrices([]); setNotice("默认价格已保存，已有工作区价格不变"); await reload();
        }); }}>
          <div className="pricing-group-heading"><h2>{pricing.name} · 默认价格</h2></div>
          <PolicyTable name="供应商默认价格" policies={prices} busy={busy} update={(p, patch) => setPrices(items => items.map(item => item.capability === p.capability ? { ...item, ...patch } : item))} />
          <div className="pricing-footer"><span>仅用于首次选用此模型的工作区</span><div className="provider-form-actions"><button className="secondary" type="button" disabled={busy} onClick={() => dirty ? setDiscard(true) : setPricing(null)}>取消</button><button className="primary" disabled={busy || !dirty || !pricesComplete}><Save size={16} />保存默认价格</button></div></div>
          {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的修改？</span><button type="button" className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className="danger" onClick={() => { setDiscard(false); setPricing(null); setPrices([]); setOriginalPrices([]); }}>放弃修改</button></div>}
        </form>}
        {editing !== null && <form className="provider-form" onSubmit={event => { event.preventDefault(); void action(save); }}>
          <h2>{editing === "new" ? "添加供应商" : "编辑供应商"}</h2>
          <fieldset disabled={busy} className="provider-form-fields">
            <div className="provider-form-grid">
              <label>标识<input required maxLength={80} pattern="[a-z0-9][a-z0-9_\x2d]*" value={form.key} onChange={e => setForm({ ...form, key: e.target.value })} placeholder="例如 qwen" /></label>
              <label>显示名称<input required maxLength={120} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
              <label className="provider-form-wide">Base URL<input required type="url" maxLength={1000} value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
              <label>模型名称<input required maxLength={200} value={form.model} onChange={e => setForm({ ...form, model: e.target.value })} /></label>
              <label>API Key<input type="password" autoComplete="new-password" required={editing === "new"} value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} placeholder={editing === "new" ? "输入供应商密钥" : "留空保留现有密钥"} /></label>
              <label>超时（秒）<input required type="number" min={1} max={600} value={form.timeout_seconds} onChange={e => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
              <label className="provider-enabled"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用供应商</label>
              <label className="provider-enabled"><input type="checkbox" checked={form.published} onChange={e => setForm({ ...form, published: e.target.checked })} />允许所有工作区选择</label>
            </div>
            <div className="provider-form-actions"><button className="primary">{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存供应商</button><button type="button" className="secondary" onClick={() => dirty ? setDiscard(true) : setEditing(null)}>取消</button></div>
          </fieldset>
          {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的修改？</span><button type="button" className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className="danger" onClick={() => { setDiscard(false); setEditing(null); setForm(blank); }}>放弃修改</button></div>}
        </form>}
        {providers.map(provider => <section className="provider-section" key={provider.id}>
          <div className="provider-section-heading"><div><div className="provider-title-row"><strong>{provider.name}</strong><span className="provider-status">{provider.enabled ? "已启用" : "已停用"}{provider.is_default ? " · 新工作区默认" : ""}</span></div><small>{provider.model} · {provider.key}</small><small>{provider.base_url}</small><small>{provider.workspace_count} 个工作区 · {provider.published ? "全站可选" : "仅原工作区"} · {provider.configured ? "默认价格已配置" : "默认价格待配置"}</small></div>
            <div className="provider-form-actions"><button className="icon-button" aria-label={`编辑${provider.name}`} title="编辑供应商" disabled={busy || editing !== null || Boolean(pricing)} onClick={() => edit(provider)}><Pencil size={16} /></button>
              <button className="secondary" disabled={busy || editing !== null || Boolean(pricing)} onClick={() => editPrices(provider)}>默认价格</button>
              {!provider.is_default && <button className="secondary" disabled={busy || editing !== null || Boolean(pricing) || !provider.enabled || !provider.published || !provider.configured} onClick={() => void action(async () => { await request(`${base}/${provider.id}/default`, json({ version: provider.version })); setNotice("新工作区默认模型已更新"); await reload(); })}>设为默认</button>}
            </div>
          </div>
        </section>)}
      </>}
    </AdminLoad>
  </section>;
}
