"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Check, Coins, Loader2, Save, Settings2, Star } from "lucide-react";
import * as Tabs from "../components/ui/tabs";
import { ApiError, request } from "./api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import { useAdminChanges } from "./admin/admin-changes";
import { availabilityLabel, type ModelProvider } from "./model-types";
import { ThinkingDiagnostics } from "./thinking-diagnostics";
import { ModelConfigFields, draftModel, modelError, modelPayload, type ModelDraft } from "./model-config-fields";
import { PolicyTable } from "./policy-table";
import type { ModelConnection } from "./model-connections-panel";
import "./billing.css";

export { ConnectionCatalog as ModelProviderPanel } from "./model-connections-panel";
const base = "/site/admin/llm-providers";

export function ModelProviderDetail({ id }: { id: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const tab = params.get("tab") === "pricing" ? "pricing" : "model";
  const resource = useAdminResource<ModelProvider>(`${base}/${encodeURIComponent(id)}`);
  const connections = useAdminResource<ModelConnection[]>("/site/admin/model-connections");
  const [provider, setProvider] = useState<ModelProvider | null>(null);
  const [form, setForm] = useState<ModelDraft>(draftModel());
  const [original, setOriginal] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingTab, setPendingTab] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  useEffect(() => {
    setProvider(null); setOriginal(""); setError(""); setNotice(""); setPendingTab(null); setConflict(false);
  }, [id]);
  const dirty = !!provider && JSON.stringify({ ...form, connectionId }) !== original;
  useAdminChanges(dirty, busy);
  function accept(value: ModelProvider) {
    const next = { ...draftModel(value.model, value.prices), name: value.name, enabled: value.enabled,
      thinking: value.thinking ?? draftModel().thinking, context_capacity: value.context_capacity ?? null,
      max_output_tokens: value.max_output_tokens ?? null };
    setProvider(value); setForm(next); setConnectionId(value.connection_id);
    setOriginal(JSON.stringify({ ...next, connectionId: value.connection_id }));
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  const selected = connections.data?.find(c => c.id === connectionId);
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(""); setNotice(""); setConflict(false);
    try { await run(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试");
      setConflict(e instanceof ApiError && e.status === 409); }
    finally { setBusy(false); }
  }
  function switchTab(next: string) {
    if (dirty) { setPendingTab(next); return; }
    router.replace(`/admin/models/${id}${next === "pricing" ? "?tab=pricing" : ""}`, { scroll: false });
  }
  async function save() {
    const issue = modelError(form);
    if (issue) { setError(issue); return; }
    if (!selected) { setError("请选择供应商连接"); return; }
    await action(async () => {
      accept(await request<ModelProvider>(`${base}/${id}`, json({ ...modelPayload(form),
        connection_id: selected.id, connection_version: selected.version, version: provider!.version }, "PUT")));
      setNotice("模型已保存");
    });
  }
  return <>
    <Link href={provider ? `/admin/models/connections/${provider.connection_id}` : "/admin/models"} className="admin-back">
      <ArrowLeft size={15} />{provider?.connection_name ?? "模型管理"}</Link>
    <AdminHeading title={provider?.name ?? "模型详情"}>
      {provider && (provider.is_default ? <span className="model-default"><Star size={14} />问答默认模型</span>
        : <button className="secondary" disabled={busy || dirty || !provider.available} onClick={() => void action(async () => {
          accept(await request<ModelProvider>(`${base}/${id}/default`, json({ version: provider.version })));
          setNotice("问答默认模型已更新");
        })}><Star size={15} />设为问答默认模型</button>)}
    </AdminHeading>
    {provider && <div className="model-detail-summary"><span className="model-availability" data-warning={!provider.available}>
      {availabilityLabel(provider.unavailable_reason)}</span><span>{provider.model}</span></div>}
    {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
    {error && <div className="admin-error" role="alert">{error}</div>}
    {conflict && <button className="secondary" onClick={() => setPendingTab("reload")}>重新加载并放弃修改</button>}
    {pendingTab && <div className="admin-inline-confirm" role="alert"><span>有尚未保存的修改，要放弃吗？</span>
      <button className="secondary" onClick={() => setPendingTab(null)}>继续编辑</button>
      <button className="danger" onClick={() => {
        const next = pendingTab; setPendingTab(null); setError(""); setConflict(false);
        if (provider) accept(provider);
        if (next === "reload") { void resource.reload(); void connections.reload(); }
        else router.replace(`/admin/models/${id}${next === "pricing" ? "?tab=pricing" : ""}`, { scroll: false });
      }}>放弃修改</button></div>}
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {provider && <form className="model-detail-form pricing-editor" onSubmit={e => { e.preventDefault(); void save(); }}>
        <Tabs.Root value={tab} onValueChange={switchTab} activationMode="manual">
          <Tabs.List className="model-detail-tabs" aria-label="模型详情">
            <Tabs.Trigger value="model" disabled={busy}><Settings2 size={17} /><strong>模型设置</strong></Tabs.Trigger>
            <Tabs.Trigger value="pricing" disabled={busy}><Coins size={17} /><strong>价格</strong></Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="model"><fieldset className="provider-form-fields" disabled={busy}>
            <div className="provider-form-grid"><label>供应商连接
              <select value={connectionId} disabled={connections.loading} onChange={e => setConnectionId(e.target.value)}>
                {!connections.data && <option value={connectionId}>{provider.connection_name}</option>}
                {connections.data?.map(c => <option key={c.id} value={c.id}>{c.name}{!c.enabled ? "（已停用）" : ""}</option>)}
              </select></label></div>
            {connections.error && <p className="form-error">{connections.error}<button type="button" className="secondary" onClick={connections.reload}>重试</button></p>}
            <ModelConfigFields value={form} change={setForm} platform={selected?.platform} apiProtocol={selected?.api_protocol} />
            <label className="provider-enabled"><input type="checkbox" checked={form.enabled}
              onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用模型</label>
          </fieldset>
          <details className="model-advanced"><summary>参数验证</summary>
            <ThinkingDiagnostics id={provider.id} version={provider.version} connectionId={selected?.id ?? provider.connection_id} connectionVersion={selected?.version ?? provider.connection_version}
              thinkingVersion={provider.thinking_version ?? 0} thinking={form.thinking} maxOutput={form.max_output_tokens} dirty={dirty} />
          </details></Tabs.Content>
          <Tabs.Content value="pricing">
            <p className="pricing-description">修改影响后续新调用，既有消费不变。免费请明确填写 0。</p>
            <PolicyTable name="模型基准价" policies={form.policies} busy={busy} requirePrices={form.enabled}
              update={(price, patch) => setForm(v => ({ ...v, policies: v.policies.map(p => p.capability === price.capability ? { ...p, ...patch } : p) }))} />
          </Tabs.Content>
        </Tabs.Root>
        <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : "已保存"}</span>
          <button className="primary" disabled={busy || !dirty || !selected}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存模型</button>
        </div>
      </form>}
    </AdminLoad>
  </>;
}
