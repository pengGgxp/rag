"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, Coins, Loader2, Plus, PlugZap, Save, Search, Star } from "lucide-react";
import * as Tabs from "../components/ui/tabs";
import { request } from "./api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useAdminChanges } from "./admin/admin-changes";
import { availabilityLabel, emptyThinking, type ModelProvider, type Policy } from "./model-types";
import { ThinkingConfigFields, thinkingConfigError } from "./thinking-config-fields";
import { ThinkingDiagnostics } from "./thinking-diagnostics";
import { PolicyTable } from "./policy-table";
import "./billing.css";

const base = "/site/admin/llm-providers";
const blank = { key: "", name: "", base_url: "", model: "", api_key: "", timeout_seconds: 60, enabled: false, version: 0,
  thinking: emptyThinking, context_capacity: null as number | null, max_output_tokens: null as number | null };
const capabilities = ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"];
type ProviderForm = typeof blank;

export function ModelProviderPanel() {
  const { data, loading, error, reload } = useAdminResource<ModelProvider[]>(base);
  const params = useSearchParams();
  const router = useRouter();
  const search = params.get("q") ?? "";
  const status = params.get("status") ?? "all";
  const [query, setQuery] = useState(search);
  useEffect(() => setQuery(search), [search]);
  useAdminChanges(false, false);
  function filter(q: string, nextStatus: string) {
    const next = new URLSearchParams();
    if (q) next.set("q", q);
    if (nextStatus !== "all") next.set("status", nextStatus);
    router.replace(`/admin/models${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  const models = data?.filter(p => `${p.name} ${p.model}`.toLowerCase().includes(search.toLowerCase())
    && (status === "available" ? p.available : status === "unavailable" ? !p.available : true));
  return <>
    <AdminHeading title="模型管理"><Link href="/admin/models/new" className="primary"><Plus size={16} />添加模型</Link></AdminHeading>
    <form className="admin-filters" onSubmit={event => { event.preventDefault(); filter(query.trim(), status); }}>
      <label>搜索模型<input value={query} onChange={event => setQuery(event.target.value)} placeholder="名称或模型名" /></label>
      <label>筛选<select value={status} onChange={event => filter(query.trim(), event.target.value)}>
        <option value="all">全部模型</option><option value="available">可用模型</option><option value="unavailable">不可用模型</option>
      </select></label>
      <button className="secondary"><Search size={16} />查询</button>
    </form>
    <AdminLoad loading={loading} error={error} retry={reload}>
      {models && <><div className="model-catalog" role="table" aria-label="模型配置列表">
        <div className="model-catalog-heading" role="row"><span role="columnheader">名称 / 模型</span><span role="columnheader">可用性</span><span role="columnheader">关联工作区</span><span role="columnheader">操作</span></div>
        {models.map(model => <div className="model-catalog-row" role="row" key={model.id}>
          <div className="model-catalog-name" role="cell"><Link href={`/admin/models/${model.id}`}>{model.name}</Link><small>{model.model}</small>
            {model.is_default && <span className="model-default"><Star size={12} />问答默认</span>}</div>
          <div role="cell" className="model-catalog-state"><span className="model-price-label">可用性</span><span className="model-availability" data-warning={!model.available}>{availabilityLabel(model.unavailable_reason)}</span></div>
          <div role="cell" className="model-catalog-state"><span className="model-price-label">关联工作区</span><span>{model.workspace_count} 个</span></div>
          <div role="cell" className="model-catalog-action"><Link className="secondary" href={`/admin/models/${model.id}`} aria-label={`管理${model.name}`}>管理<ArrowRight size={15} /></Link></div>
        </div>)}
      </div>{!models.length && <p className="admin-empty">{data?.length ? "没有符合条件的模型" : "尚未添加模型"}</p>}
      <p className="billing-muted">共 {models.length} 个模型{search || status !== "all" ? `，全部 ${data?.length ?? 0} 个` : ""}</p></>}
    </AdminLoad>
  </>;
}

function connectionForm(provider: ModelProvider): ProviderForm {
  return { key: provider.key, name: provider.name, base_url: provider.base_url, model: provider.model,
    api_key: "", timeout_seconds: provider.timeout_seconds, enabled: provider.enabled, version: provider.version,
    thinking: { ...emptyThinking, ...provider.thinking }, context_capacity: provider.context_capacity ?? null,
    max_output_tokens: provider.max_output_tokens ?? null };
}

function priceForm(provider: ModelProvider): Policy[] {
  return capabilities.map(capability => ({
    capability, unit: "tokens", unit_size: 1000, credits_per_unit: null, enabled: true, version: provider.version,
    ...provider.prices.find(p => p.capability === capability),
    configured: provider.prices.some(p => p.capability === capability),
  }));
}

export function ModelProviderDetail({ id }: { id: string }) {
  const isNew = id === "new";
  const router = useRouter();
  const params = useSearchParams();
  const tab = !isNew && params.get("tab") === "pricing" ? "pricing" : "connection";
  const resource = useAdminResource<ModelProvider | null>(isNew ? null : `${base}/${encodeURIComponent(id)}`);
  const [provider, setProvider] = useState<ModelProvider | null>(null);
  const [form, setForm] = useState<ProviderForm>(blank);
  const [original, setOriginal] = useState<ProviderForm>(blank);
  const [prices, setPrices] = useState<Policy[]>([]);
  const [originalPrices, setOriginalPrices] = useState<Policy[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingTab, setPendingTab] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original) || JSON.stringify(prices) !== JSON.stringify(originalPrices);
  useAdminChanges(dirty, busy);
  function accept(value: ModelProvider) {
    const connection = connectionForm(value);
    const pricing = priceForm(value);
    setProvider(value); setForm(connection); setOriginal(connection); setPrices(pricing); setOriginalPrices(pricing);
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  function changeTab(next: string) {
    if (busy || next === tab) return;
    if (dirty) { setPendingTab(next); return; }
    setError(""); setNotice("");
    router.replace(`/admin/models/${id}${next === "pricing" ? "?tab=pricing" : ""}`, { scroll: false });
  }
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(""); setNotice(""); setConflict(false);
    try { await run(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); setConflict(true); }
    finally { setBusy(false); }
  }
  async function saveConnection() {
    const { api_key, ...fields } = form;
    const updated = await request<ModelProvider>(isNew ? base : `${base}/${id}`,
      json({ ...fields, ...(api_key ? { api_key } : {}) }, isNew ? "POST" : "PUT"));
    accept(updated);
    if (isNew) router.replace(`/admin/models/${updated.id}?tab=pricing`);
    else setNotice("连接配置已保存");
  }
  const complete = prices.length === 3 && prices.every(p => p.credits_per_unit !== null
    && Number.isInteger(p.credits_per_unit) && p.credits_per_unit >= 0 && Number.isInteger(p.unit_size) && p.unit_size > 0 && p.enabled);
  const budgetError = (form.context_capacity === null) !== (form.max_output_tokens === null)
    ? "请同时填写上下文容量和最大输出，或同时留空。"
    : form.context_capacity && form.max_output_tokens && form.max_output_tokens >= form.context_capacity * 0.95
      ? "最大输出必须小于上下文容量减去 5% 安全余量。" : "";
  return <>
    <Link href="/admin/models" className="admin-back"><ArrowLeft size={15} />模型管理</Link>
    <AdminHeading title={isNew ? "添加模型" : provider?.name ?? "模型详情"}>
      {provider && <span className="model-detail-actions">
        {provider.is_default ? <span className="model-default"><Star size={14} />问答默认模型</span>
          : <button className="secondary" disabled={busy || dirty || !provider.available} onClick={() => void action(async () => {
            accept(await request<ModelProvider>(`${base}/${id}/default`, json({ version: provider.version })));
            setNotice("问答默认模型已更新");
          })}><Star size={15} />设为问答默认模型</button>}
      </span>}
    </AdminHeading>
    {provider && <div className="model-detail-summary"><span className="model-availability" data-warning={!provider.available}>{availabilityLabel(provider.unavailable_reason)}</span>
      <span>适用所有工作区</span><span>已使用的工作区 {provider.workspace_count} 个</span></div>}
    {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
    {error && <div className="admin-error" role="alert">{error}</div>}
    {conflict && !isNew && <div className="model-reload"><button className="secondary" disabled={busy} onClick={() => setPendingTab("reload")}>重新加载并放弃修改</button></div>}
    {pendingTab && <div className="admin-inline-confirm" role="alert"><span>有尚未保存的修改，要放弃吗？</span>
      <button className="secondary" onClick={() => setPendingTab(null)}>继续编辑</button>
      <button className="danger" onClick={() => {
        const next = pendingTab; setPendingTab(null); setError(""); setConflict(false);
        if (provider) accept(provider);
        if (next === "reload") void resource.reload();
        else router.replace(`/admin/models/${id}${next === "pricing" ? "?tab=pricing" : ""}`, { scroll: false });
      }}>放弃修改</button>
    </div>}
    <AdminLoad loading={!isNew && resource.loading} error={isNew ? "" : resource.error} retry={resource.reload}>
      {(isNew || provider) && <Tabs.Root value={tab} onValueChange={changeTab} activationMode="manual">
        <Tabs.List className="model-detail-tabs" aria-label="模型详情">
          <Tabs.Trigger value="connection" disabled={busy} aria-label="连接配置">
            <PlugZap size={17} aria-hidden="true" /><span><strong>连接配置</strong><small>接口地址、凭据与超时</small></span>
          </Tabs.Trigger>
          <Tabs.Trigger value="pricing" disabled={busy || isNew} aria-label="模型基准价">
            <Coins size={17} aria-hidden="true" /><span><strong>模型基准价</strong><small>输入、缓存读取与输出价格</small></span>
          </Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="connection">
          <form className="provider-form model-detail-form" onSubmit={event => { event.preventDefault(); void action(saveConnection); }}>
            <fieldset disabled={busy} className="provider-form-fields">
              <section className="model-form-section"><h2>基本信息</h2><div className="provider-form-grid">
                <label>显示名称<input required maxLength={120} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
                <label>模型名<input required maxLength={200} value={form.model} onChange={e => setForm({ ...form, model: e.target.value })} /></label>
                <label>配置标识<input required maxLength={80} pattern="[a-z0-9][a-z0-9_\x2d]*" value={form.key} onChange={e => setForm({ ...form, key: e.target.value })} placeholder="例如 deepseek-flash" /></label>
              </div></section>
              <section className="model-form-section"><h2>连接凭据</h2><div className="provider-form-grid">
                <label className="provider-form-wide">接口地址<input required type="url" maxLength={1000} value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
                <label><span className="provider-key-label">API Key{!isNew && <small>{provider?.has_api_key ? "已配置，留空保留" : "尚未配置"}</small>}</span><input type="password" autoComplete="new-password" required={isNew} value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} /></label>
                <label>超时（秒）<input required type="number" min={1} max={600} value={form.timeout_seconds} onChange={e => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
              </div></section>
              <ThinkingConfigFields value={form.thinking} onChange={thinking => setForm({ ...form, thinking })} />
              {provider && <ThinkingDiagnostics id={provider.id} version={provider.version}
                thinkingVersion={provider.thinking_version ?? 0} thinking={form.thinking}
                maxOutput={form.max_output_tokens} dirty={dirty} />}
              <section className="model-form-section"><h2>上下文预算</h2>
                <p className="billing-muted model-budget-description">按接口提供方的实际规格填写，两项一同配置。留空保持上游默认，但无法启用上下文保护。</p>
                <div className="provider-form-grid">
                  <label>上下文容量（Token）<input type="number" min={1024} max={100000000} step={1}
                    value={form.context_capacity ?? ""} onChange={e => setForm({ ...form, context_capacity: e.target.value ? Number(e.target.value) : null })} /></label>
                  <label>最大输出（Token）<input type="number" min={1} max={10000000} step={1}
                    value={form.max_output_tokens ?? ""} onChange={e => setForm({ ...form, max_output_tokens: e.target.value ? Number(e.target.value) : null })} /></label>
                </div>
                {budgetError && <p className="form-error" role="alert">{budgetError}</p>}
              </section>
              <section className="model-form-section"><h2>运行状态</h2><div className="model-scope-fields">
                <label className="provider-enabled"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用模型</label>
              </div>{isNew && <p className="billing-muted">先保存连接，再补齐三项模型基准价后启用。</p>}</section>
              <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : "连接配置"}{provider ? ` · 版本 ${provider.version}` : ""}</span>
                <button className="primary" disabled={busy || !dirty || !!thinkingConfigError(form.thinking) || !!budgetError}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}{isNew ? "保存并配置基准价" : "保存连接配置"}</button></div>
            </fieldset>
          </form>
        </Tabs.Content>
        <Tabs.Content value="pricing">
          <form className="pricing-editor model-detail-form" onSubmit={event => { event.preventDefault(); void action(async () => {
            const updated = await request<ModelProvider>(`${base}/${id}/pricing`, json({ version: provider!.version,
              policies: prices.map(({ capability, unit, unit_size, credits_per_unit, enabled }) => ({ capability, unit, unit_size, credits_per_unit, enabled })) }, "PUT"));
            accept(updated); setNotice("模型基准价已保存，后续新调用使用新价格");
          }); }}>
            <p className="pricing-description">以下为共享模型基准价，工作区按各自倍率计算。修改影响后续新调用，不改变已记录消费；免费请明确填写 0。</p>
            <PolicyTable name="模型基准价" policies={prices} busy={busy} update={(price, patch) => setPrices(items => items.map(item => item.capability === price.capability ? { ...item, ...patch } : item))} />
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : "当前基准价"} · 版本 {provider?.version}</span>
              <button className="primary" disabled={busy || !dirty || !complete}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存模型基准价</button></div>
          </form>
        </Tabs.Content>
      </Tabs.Root>}
    </AdminLoad>
  </>;
}
