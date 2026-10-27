"use client";

import { useState } from "react";
import { Loader2, Pencil, Plus, Save } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useWorkspaceChanges } from "./admin/workspace-target";
import type { ModelProvider } from "./model-types";
import "./billing.css";

const blank = { key: "", name: "", base_url: "", model: "", api_key: "", timeout_seconds: 60, enabled: true };
type ProviderForm = typeof blank;

export function ModelProviderPanel({ workspaceId }: { workspaceId: string }) {
  const base = `/site/admin/workspaces/${encodeURIComponent(workspaceId)}/llm-providers`;
  const { data: providers, loading, error: loadError, reload } = useAdminResource<ModelProvider[]>(base);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderForm>(blank);
  const [original, setOriginal] = useState<ProviderForm>(blank);
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = editing !== null && JSON.stringify(form) !== JSON.stringify(original);
  useWorkspaceChanges(dirty, busy);
  function edit(provider?: ModelProvider) {
    const value = provider ? { key: provider.key, name: provider.name, base_url: provider.base_url, model: provider.model, api_key: "", timeout_seconds: provider.timeout_seconds, enabled: provider.enabled } : blank;
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
  return <section aria-label="工作区模型供应商">
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    <AdminLoad loading={loading} error={loadError} retry={reload}>
      {providers && <>
        {!providers.some(item => item.active && item.enabled) && <p className="billing-muted">当前使用平台默认模型配置。</p>}
        {editing === null && <button className="secondary" onClick={() => edit()}><Plus size={16} />添加供应商</button>}
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
            </div>
            <div className="provider-form-actions"><button className="primary">{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存供应商</button><button type="button" className="secondary" onClick={() => dirty ? setDiscard(true) : setEditing(null)}>取消</button></div>
          </fieldset>
          {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的修改？</span><button type="button" className="secondary" onClick={() => setDiscard(false)}>继续编辑</button><button type="button" className="danger" onClick={() => { setDiscard(false); setEditing(null); setForm(blank); }}>放弃修改</button></div>}
        </form>}
        {providers.map(provider => <section className="provider-section" key={provider.id}>
          <div className="provider-section-heading"><div><div className="provider-title-row"><strong>{provider.name}</strong><span className="provider-status">{provider.active && provider.enabled ? "当前使用" : provider.enabled ? "已启用" : "已停用"}</span></div><small>{provider.model} · {provider.key}</small><small>{provider.base_url}</small><small>{provider.has_api_key ? "密钥已配置" : "密钥未配置"} · 超时 {provider.timeout_seconds} 秒</small></div>
            <div className="provider-form-actions"><button className="icon-button" aria-label={`编辑${provider.name}`} title="编辑供应商" disabled={busy || editing !== null} onClick={() => edit(provider)}><Pencil size={16} /></button>
              {!provider.active && provider.enabled && <button className="secondary" disabled={busy || editing !== null} onClick={() => void action(async () => { await request(`${base}/${provider.id}/activate`, { method: "POST" }); setNotice(`已切换到 ${provider.name}`); await reload(); })}>设为当前</button>}
            </div>
          </div>
        </section>)}
      </>}
    </AdminLoad>
  </section>;
}
