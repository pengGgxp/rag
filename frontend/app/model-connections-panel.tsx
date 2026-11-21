"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ImageModelSettings } from "./image-model-settings";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, ChevronDown, Download, Layers, Loader2, Plus, PlugZap, Save, Search, Star, X } from "lucide-react";
import { Tooltip } from "../components/ui/tooltip";
import * as Tabs from "../components/ui/tabs";
import { ApiError, request } from "./api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useAdminChanges } from "./admin/admin-changes";
import { availabilityLabel, type ChatProtocol, type ModelProvider, type Policy } from "./model-types";
import { ModelConfigFields, draftModel, modelError, modelPayload, type ModelDraft } from "./model-config-fields";
import { PolicyTable } from "./policy-table";
import "./billing.css";

export type ModelConnection = {
  id: string; name: string; platform: string; base_url: string; timeout_seconds: number;
  api_protocol: ChatProtocol; website_url: string;
  has_api_key: boolean; enabled: boolean; version: number; model_count: number; available_model_count: number;
};
type Platform = { id: string; name: string; base_url: string; adapter: string };
const base = "/site/admin/model-connections";
const platformNames: Record<string, string> = { deepseek: "DeepSeek", bailian: "阿里云百炼", openai: "OpenAI", anthropic: "Anthropic", gemini: "Google Gemini", custom: "自定义兼容接口" };
const protocolNames = { chat_completions: "OpenAI Chat Completions", responses: "OpenAI Responses", anthropic: "Anthropic Messages", gemini: "Gemini 原生" };

export function ConnectionCatalog() {
  const resource = useAdminResource<ModelConnection[]>(base);
  const router = useRouter(), params = useSearchParams();
  const search = params.get("q") ?? "", status = params.get("status") ?? "all";
  const [query, setQuery] = useState(search);
  useEffect(() => setQuery(search), [search]);
  const [imageState, setImageState] = useState({ dirty: false, busy: false });
  const imageStateChanged = useCallback((dirty: boolean, busy: boolean) => setImageState({ dirty, busy }), []);
  useAdminChanges(imageState.dirty, imageState.busy);
  function filter(q: string, state: string) {
    const next = new URLSearchParams();
    if (q) next.set("q", q);
    if (state !== "all") next.set("status", state);
    router.replace(`/admin/models${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  const items = resource.data?.filter(c => `${c.name} ${platformNames[c.platform]}`.toLowerCase().includes(search.toLowerCase())
    && (status === "enabled" ? c.enabled : status === "disabled" ? !c.enabled : true));
  return <>
    <AdminHeading title="模型管理"><Link href="/admin/models/new" className="primary"><Plus size={16} />添加供应商</Link></AdminHeading>
    <form className="admin-filters" onSubmit={e => { e.preventDefault(); filter(query.trim(), status); }}>
      <label>搜索供应商<input placeholder="名称或平台" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <label>状态<select value={status} onChange={e => filter(query.trim(), e.target.value)}>
        <option value="all">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option>
      </select></label><button className="secondary"><Search size={16} />查询</button>
    </form>
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      <div className="model-catalog" role="table" aria-label="供应商列表">
        <div className="model-catalog-heading" role="row"><span role="columnheader">供应商 / 平台</span><span role="columnheader">可用模型</span>
          <span role="columnheader">状态</span><span role="columnheader">操作</span></div>
        {items?.map(c => <div className="model-catalog-row" role="row" key={c.id}>
          <div className="model-catalog-name" role="cell"><Link href={`/admin/models/connections/${c.id}`}>{c.name}</Link><small>{platformNames[c.platform]} · {protocolNames[c.api_protocol]}</small></div>
          <div role="cell" className="model-catalog-state"><span className="model-price-label">可用模型</span>{c.available_model_count} / {c.model_count}</div>
          <div role="cell" className="model-catalog-state"><span className="model-price-label">状态</span><span className="model-availability" data-warning={!c.enabled}>{c.enabled ? "已启用" : "已停用"}</span></div>
          <div role="cell" className="model-catalog-action"><Link className="secondary" href={`/admin/models/connections/${c.id}`}>管理<ArrowRight size={15} /></Link></div>
        </div>)}
      </div>
      {items?.length === 0 && <p className="admin-empty">{resource.data?.length ? "没有符合条件的供应商" : "尚未添加供应商"}</p>}
    </AdminLoad>
    <ImageModelSettings state={imageStateChanged} />
  </>;
}

const blank = { name: "", platform: "custom", api_protocol: "", website_url: "", base_url: "", api_key: "", timeout_seconds: 60, enabled: true, version: 0 };
export function ConnectionDetail({ id }: { id: string }) {
  const isNew = id === "new", router = useRouter(), params = useSearchParams();
  const resource = useAdminResource<ModelConnection>(isNew ? null : `${base}/${encodeURIComponent(id)}`);
  const platforms = useAdminResource<Platform[]>(`${base}/platforms`);
  const models = useAdminResource<ModelProvider[]>(isNew ? null : "/site/admin/llm-providers");
  const tab = isNew || params.get("tab") === "connection" ? "connection" : "models";
  const adding = !isNew && params.get("add") === "1";
  const [form, setForm] = useState(blank), [original, setOriginal] = useState(blank);
  const [connection, setConnection] = useState<ModelConnection | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [pending, setPending] = useState<string | null>(null), [conflict, setConflict] = useState(false);
  const [modelsDirty, setModelsDirty] = useState(false);
  const [modelsBusy, setModelsBusy] = useState(false), [importEpoch, setImportEpoch] = useState(0);
  useEffect(() => {
    setConnection(null); setForm(blank); setOriginal(blank); setError(""); setNotice("");
    setPending(null); setConflict(false); setModelsDirty(false);
  }, [id]);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  useAdminChanges(dirty || modelsDirty, busy || modelsBusy);
  function accept(c: ModelConnection) {
    const next = { name: c.name, platform: c.platform, api_protocol: c.api_protocol, website_url: c.website_url ?? "", base_url: c.base_url, api_key: "",
      timeout_seconds: c.timeout_seconds, enabled: c.enabled, version: c.version };
    setConnection(c); setForm(next); setOriginal(next);
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  function navigate(next: string) {
    if (dirty || modelsDirty) { setPending(next); return; }
    router.replace(`/admin/models/connections/${id}${next === "connection" ? "?tab=connection" : ""}`, { scroll: false });
  }
  function platformChange(platform: string) {
    const preset = platforms.data?.find(p => p.id === platform);
    const previous = platforms.data?.find(p => p.id === form.platform);
    setForm(v => ({ ...v, platform, name: v.name || preset?.name || "",
      base_url: !v.base_url || v.base_url === previous?.base_url ? preset?.base_url ?? "" : v.base_url }));
  }
  async function save() {
    if (!form.api_protocol) { setError("请选择调用类型"); return; }
    setBusy(true); setError(""); setNotice(""); setConflict(false);
    try {
      const { api_key, ...fields } = form;
      const saved = await request<ModelConnection>(isNew ? base : `${base}/${id}`,
        json({ ...fields, ...(api_key ? { api_key } : {}) }, isNew ? "POST" : "PUT"));
      accept(saved);
      if (isNew) router.replace(`/admin/models/connections/${saved.id}?add=1`);
      else setNotice("供应商连接已保存");
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败，请重试"); setConflict(e instanceof ApiError && e.status === 409); }
    finally { setBusy(false); }
  }
  const ownModels = models.data?.filter(m => m.connection_id === id) ?? [];
  return <>
    <Link href="/admin/models" className="admin-back"><ArrowLeft size={15} />模型管理</Link>
    <AdminHeading title={isNew ? "添加供应商" : connection?.name ?? "供应商详情"}>
      {!isNew && !adding && tab === "models" && <Link className="primary" href={`/admin/models/connections/${id}?add=1`}><Plus size={16} />添加模型</Link>}
    </AdminHeading>
    {isNew && <ol className="connection-steps" aria-label="添加进度"><li aria-current="step">1 连接供应商</li><li>2 选择模型</li><li>3 配置并启用</li></ol>}
    {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
    {error && <div className="admin-error" role="alert">{error}</div>}
    {conflict && <button className="secondary" onClick={() => setPending("reload")}>重新加载并放弃修改</button>}
    {pending && <div className="admin-inline-confirm" role="alert"><span>有尚未保存的修改，要放弃吗？</span>
      <button className="secondary" onClick={() => setPending(null)}>继续编辑</button>
      <button className="danger" onClick={() => {
        const next = pending; setPending(null); setModelsDirty(false); setError(""); setConflict(false);
        if (connection) accept(connection);
        if (next === "reload") { setImportEpoch(value => value + 1); void resource.reload(); void models.reload(); }
        else router.replace(`/admin/models/connections/${id}${next === "connection" ? "?tab=connection" : ""}`);
      }}>放弃修改</button></div>}
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {(isNew || connection) && <Tabs.Root value={tab} onValueChange={navigate} activationMode="manual">
        {!isNew && <Tabs.List className="model-detail-tabs" aria-label="供应商详情">
          <Tabs.Trigger value="models" disabled={busy || modelsBusy}><Layers size={17} /><strong>模型</strong><span className="connection-count">{connection?.model_count}</span></Tabs.Trigger>
          <Tabs.Trigger value="connection" disabled={busy || modelsBusy}><PlugZap size={17} /><strong>连接设置</strong></Tabs.Trigger>
        </Tabs.List>}
        <Tabs.Content value="connection">
          <form className="provider-form model-detail-form" onSubmit={e => { e.preventDefault(); void save(); }}>
            <fieldset className="provider-form-fields" disabled={busy}>
              <section className="model-form-section"><h2>新模型默认协议</h2>
                <div className="provider-form-grid"><label>对话协议<select required value={form.api_protocol}
                  onChange={e => setForm({ ...form, api_protocol: e.target.value })}>
                  <option value="" disabled>请选择调用类型</option>
                  <option value="chat_completions">OpenAI Chat Completions</option>
                  <option value="responses">OpenAI Responses</option>
                  <option value="anthropic">Anthropic Messages</option>
                  <option value="gemini">Gemini 原生</option>
                </select></label></div>
              </section>
              {form.api_protocol && <>
              <section className="model-form-section"><h2>平台与名称</h2>
                <AdminLoad loading={platforms.loading} error={platforms.error} retry={platforms.reload}>
                  <div className="connection-platforms" role="group" aria-label="平台">
                    {platforms.data?.map(p => <button type="button" className="secondary" key={p.id} aria-pressed={form.platform === p.id}
                      onClick={() => platformChange(p.id)}>{p.name}</button>)}
                  </div>
                </AdminLoad>
                <div className="provider-form-grid"><label>供应商名称<input required maxLength={120} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label></div>
              </section>
              <section className="model-form-section"><h2>连接凭据</h2><div className="provider-form-grid">
                <label className="provider-form-wide">官网地址<input type="url" maxLength={1000} value={form.website_url} onChange={e => setForm({ ...form, website_url: e.target.value })} /></label>
                {connection?.website_url && <a href={connection.website_url} target="_blank" rel="noreferrer">供应商官网</a>}
                <label className="provider-form-wide">接口地址<input type="url" required maxLength={1000} value={form.base_url}
                  placeholder="https://api.example.com/v1" onChange={e => setForm({ ...form, base_url: e.target.value })} /></label>
                <label><span className="provider-key-label">API Key<small>{connection?.has_api_key ? "已配置，留空保留" : "尚未配置"}</small></span>
                  <input type="password" autoComplete="new-password" value={form.api_key} required={form.enabled && !connection?.has_api_key}
                    onChange={e => setForm({ ...form, api_key: e.target.value })} /></label>
              </div>
              <details className="model-advanced"><summary>高级连接设置</summary><div className="provider-form-grid">
                <label>超时（秒）<input type="number" min={1} max={600} required value={form.timeout_seconds}
                  onChange={e => setForm({ ...form, timeout_seconds: Number(e.target.value) })} /></label>
              </div></details></section>
              <section className="model-form-section"><label className="provider-enabled"><input type="checkbox" checked={form.enabled}
                onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用供应商</label>
                {!form.enabled && <p className="billing-muted">停用后，该供应商下所有模型将暂停新调用。</p>}
              </section>
              </>}
              <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : isNew ? "" : "已保存"}</span>
                <button className="primary" disabled={busy || !form.api_protocol || (!isNew && !dirty)}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}{isNew ? "保存并选择模型" : "保存连接"}</button></div>
            </fieldset>
          </form>
        </Tabs.Content>
        <Tabs.Content value="models">
          {connection && adding ? <AdminLoad loading={models.loading} error={models.error} retry={models.reload}>
            {!models.error && <ModelImport key={`${connection.id}-${connection.version}-${importEpoch}`} connection={connection} existing={ownModels}
            dirty={setModelsDirty} working={setModelsBusy} reload={() => setPending("reload")}
            finished={() => { setModelsDirty(false); void models.reload(); void resource.reload(); router.replace(`/admin/models/connections/${id}`); }} />}
            </AdminLoad>
            : <AdminLoad loading={models.loading} error={models.error} retry={models.reload}>
              <ConnectionModels models={ownModels} />
            </AdminLoad>}
        </Tabs.Content>
      </Tabs.Root>}
    </AdminLoad>
  </>;
}

function ConnectionModels({ models }: { models: ModelProvider[] }) {
  return <div className="model-catalog" role="table" aria-label="模型列表">
    <div className="model-catalog-heading" role="row"><span role="columnheader">名称 / 模型</span><span role="columnheader">可用性</span>
      <span role="columnheader">使用账号</span><span role="columnheader">操作</span></div>
    {models.map(m => <div className="model-catalog-row" role="row" key={m.id}>
      <div role="cell" className="model-catalog-name"><Link href={`/admin/models/${m.id}`}>{m.name}</Link><small>{m.model}</small>
        {m.is_default && <span className="model-default"><Star size={12} />问答默认</span>}</div>
      <div role="cell" className="model-catalog-state"><span className="model-price-label">可用性</span><span className="model-availability" data-warning={!m.available}>{availabilityLabel(m.unavailable_reason)}</span></div>
      <div role="cell" className="model-catalog-state"><span className="model-price-label">使用账号</span>{m.user_count} 个</div>
      <div role="cell" className="model-catalog-action"><Link className="secondary" href={`/admin/models/${m.id}`}>配置<ArrowRight size={15} /></Link></div>
    </div>)}
    {!models.length && <p className="admin-empty">尚未添加模型</p>}
  </div>;
}

function ModelImport({ connection, existing, dirty, working, reload, finished }: {
  connection: ModelConnection; existing: ModelProvider[]; dirty: (value: boolean) => void;
  working: (value: boolean) => void; reload: () => void; finished: () => void;
}) {
  const template = useAdminResource<{ policies: Policy[] }>("/site/admin/pricing-template");
  const [candidates, setCandidates] = useState<string[]>([]), [query, setQuery] = useState(""), [manual, setManual] = useState("");
  const [drafts, setDrafts] = useState<ModelDraft[]>([]), [stage, setStage] = useState("select");
  const [busy, setBusy] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [invalid, setInvalid] = useState(""), [conflict, setConflict] = useState(false);
  const sequence = useRef(0);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!invalid) return;
    const section = container.current?.querySelector<HTMLDetailsElement>('[data-invalid="true"]');
    if (!section) return;
    section.open = true;
    section.scrollIntoView({ block: "nearest" });
    section.querySelector<HTMLElement>('input:invalid, input[data-missing="true"], summary')?.focus();
  }, [invalid, error]);
  useEffect(() => { dirty(drafts.length > 0); }, [drafts.length, dirty]);
  useEffect(() => { working(!!busy); return () => working(false); }, [busy, working]);
  useEffect(() => () => { sequence.current++; }, []);
  const existingNames = new Set(existing.map(m => m.model));
  function select(model: string) {
    if (existingNames.has(model)) return;
    setDrafts(rows => rows.some(d => d.model === model) ? rows.filter(d => d.model !== model)
      : rows.length >= 50 ? rows : [...rows, { ...draftModel(model, template.data?.policies), chat_protocol: connection.api_protocol }]);
    setError("");
  }
  async function discover() {
    const seq = ++sequence.current; setBusy("discover"); setError(""); setNotice("");
    try {
      const result = await request<{ models: string[] }>(`${base}/${connection.id}/models/discover`, json({ version: connection.version }));
      if (seq !== sequence.current) return;
      setCandidates(result.models);
      if (!result.models.length) setNotice("未获取到模型，可以手动添加。");
    } catch (e) { if (seq === sequence.current) { setError(e instanceof Error ? e.message : "获取失败，请手动添加");
      setConflict(e instanceof ApiError && e.status === 409); } }
    finally { if (seq === sequence.current) setBusy(""); }
  }
  async function save(enable: boolean) {
    setError(""); setInvalid("");
    const bad = drafts.find(d => modelError(d, enable));
    if (bad) { setInvalid(bad.model); setError(`${bad.model}：${modelError(bad, enable)}`); return; }
    setBusy("save");
    try {
      await request(`${base}/${connection.id}/models`, json({ connection_version: connection.version,
        models: drafts.map(d => ({ ...modelPayload(d), enabled: enable, connection_id: connection.id,
          connection_version: connection.version })) }));
      finished();
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败，请重试");
      setConflict(e instanceof ApiError && e.status === 409); }
    finally { setBusy(""); }
  }
  const filtered = candidates.filter(m => m.toLowerCase().includes(query.toLowerCase()));
  return <div className="model-import pricing-editor" ref={container}>
    <ol className="connection-steps" aria-label="添加进度"><li>1 连接已保存</li><li aria-current={stage === "select" ? "step" : undefined}>2 选择模型</li>
      <li aria-current={stage === "configure" ? "step" : undefined}>3 配置并启用</li></ol>
    <AdminLoad loading={template.loading} error={template.error} retry={template.reload}>
      {error && <div className="admin-error" role="alert">{error}</div>}
      {notice && <p role="status" className="billing-muted">{notice}</p>}
      {conflict && <button className="secondary" onClick={reload}>重新加载并放弃修改</button>}
      {stage === "select" ? <>
        <div className="model-import-toolbar"><h2>选择要添加的模型</h2><button className="secondary" disabled={!!busy || !template.data} onClick={() => void discover()}>
          {busy === "discover" ? <Loader2 size={16} className="spin" /> : <Download size={16} />}获取模型列表</button></div>
        <div className="provider-form-grid"><label>搜索模型<input value={query} placeholder="模型名" onChange={e => setQuery(e.target.value)} /></label></div>
        {candidates.length > 0 && <div className="model-candidates" aria-label="候选模型">
          {filtered.map(m => <label className="model-candidate" key={m}><input type="checkbox" disabled={!!busy || existingNames.has(m)}
            checked={existingNames.has(m) || drafts.some(d => d.model === m)} onChange={() => select(m)} /><span>{m}</span>{existingNames.has(m) && <small>已添加</small>}</label>)}
          {!filtered.length && <p className="admin-empty">没有匹配的模型</p>}
        </div>}
        <form className="model-manual" onSubmit={e => { e.preventDefault(); const m = manual.trim(); if (!m) return;
          if (existingNames.has(m) || drafts.some(d => d.model === m)) { setError("该模型已添加"); return; }
          select(m); setManual(""); }}>
          <label>手动添加模型<input required maxLength={200} value={manual} placeholder="填写接口中的完整模型名" onChange={e => setManual(e.target.value)} /></label>
          <button className="secondary" disabled={!!busy || !template.data || drafts.length >= 50}><Plus size={16} />添加</button>
        </form>
        <div className="model-selection" aria-label="已选模型">{drafts.map(d => <span key={d.model}>{d.model}<Tooltip content="移除模型"><button className="icon-button"
          aria-label={`移除${d.model}`} onClick={() => select(d.model)}><X size={14} /></button></Tooltip></span>)}</div>
        <div className="pricing-footer"><span>已选 {drafts.length} / 50 个</span>
          <button className="primary" disabled={!!busy || !drafts.length || !template.data} onClick={() => setStage("configure")}>配置模型<ArrowRight size={16} /></button></div>
      </> : <>
        <div className="model-import-toolbar"><h2>配置模型</h2><button className="secondary" disabled={!!busy} onClick={() => setStage("select")}><ArrowLeft size={15} />返回选择</button></div>
        {drafts.map((d, index) => <details className="model-draft" key={index} data-invalid={invalid === d.model}
          open={invalid ? invalid === d.model : index === 0 ? true : undefined}>
          <summary><ChevronDown size={16} aria-hidden="true" /><span>{d.name || d.model}</span><small>{modelError(d, true) ? "待完善" : "可启用"}</small></summary>
          <fieldset className="provider-form-fields" disabled={!!busy}>
            <ModelConfigFields value={d} change={value => setDrafts(rows => rows.map((r, i) => i === index ? value : r))} platform={connection.platform} apiProtocol={d.chat_protocol} />
            {d.supports_chat && <><h3>模型基准价</h3>
            <PolicyTable name={`${d.model}基准价`} policies={d.policies} busy={!!busy} requirePrices={false}
              update={(price, patch) => setDrafts(rows => rows.map((r, i) => i === index ? { ...r,
                policies: r.policies.map(p => p.capability === price.capability ? { ...p, ...patch } : p) } : r))} /></>}
          </fieldset>
        </details>)}
        <div className="pricing-footer"><span>共 {drafts.length} 个模型</span><div className="model-save-actions">
          <button className="secondary" disabled={!!busy} onClick={() => void save(false)}><Save size={16} />保存草稿</button>
          <button className="primary" disabled={!!busy || !connection.enabled} onClick={() => void save(true)}>
            {busy === "save" ? <Loader2 size={16} className="spin" /> : <Check size={16} />}保存并启用</button></div></div>
        {!connection.enabled && <p className="form-error">供应商已停用，可先保存草稿。</p>}
      </>}
    </AdminLoad>
  </div>;
}
