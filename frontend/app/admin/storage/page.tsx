"use client";

import { useEffect, useState } from "react";
import { Check, HardDrive, Loader2, PlugZap, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import "../../billing.css";
import * as Tabs from "../../../components/ui/tabs";
import { QuotaSettings } from "./quota-settings";

export default function StoragePage() {
  const [tab, setTab] = useState("quotas");
  const [state, setState] = useState({ dirty: false, busy: false });
  const [pending, setPending] = useState("");
  return <><AdminHeading title="文件存储" />
    {pending && <div className="admin-inline-confirm" role="alert"><span>有尚未保存的修改，要放弃吗？</span><button className="secondary" onClick={() => setPending("")}>继续编辑</button><button className="danger" onClick={() => { setState({dirty:false,busy:false}); setTab(pending); setPending(""); }}>放弃修改</button></div>}
    <Tabs.Root value={tab} onValueChange={value => { if (state.busy) return; if (state.dirty) setPending(value); else setTab(value); }}>
    <Tabs.List className="model-detail-tabs quota-tabs" aria-label="存储设置"><Tabs.Trigger value="connection"><PlugZap size={18} />存储连接</Tabs.Trigger><Tabs.Trigger value="quotas"><HardDrive size={18} />额度与套餐</Tabs.Trigger></Tabs.List>
    <Tabs.Content value="connection"><StorageConnection onState={setState} /></Tabs.Content><Tabs.Content value="quotas"><QuotaSettings onState={setState} /></Tabs.Content>
  </Tabs.Root></>;
}

type Storage = {
  provider: "cos" | "oss";
  version: number; bucket: string; region: string; has_secret_id: boolean; has_secret_key: boolean;
  secret_id_masked: string; active: boolean; tested: boolean; test_result: string | null;
};
type StorageConnections = { active_provider: "cos" | "oss" | null; connections: Storage[] };
const initial = { bucket: "", region: "ap-guangzhou", secret_id: "", secret_key: "", clear_credentials: false };
function StorageConnection({ onState }: { onState: (state: { dirty: boolean; busy: boolean }) => void }) {
  const resource = useAdminResource<StorageConnections>("/site/admin/storage");
  const [provider, setProvider] = useState<"cos" | "oss">("cos");
  const [connections, setConnections] = useState<Storage[]>([]);
  const [pendingProvider, setPendingProvider] = useState<"cos" | "oss" | null>(null);
  const [config, setConfig] = useState<Storage | null>(null);
  const [form, setForm] = useState(initial);
  const [original, setOriginal] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  useAdminChanges(dirty, busy);
  useEffect(() => { onState({ dirty, busy }); }, [dirty, busy, onState]);
  function accept(value: Storage) {
    setConfig(value);
    const next = { ...initial, bucket: value.bucket, region: value.region };
    setForm(next); setOriginal(next);
  }
  useEffect(() => {
    if (resource.data) {
      setConnections(resource.data.connections);
      const value = resource.data.connections.find(item => item.provider === provider);
      if (value) accept(value);
    }
    // Reload explicitly replaces the selected connection after conflict confirmation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resource.data]);
  function selectProvider(value: "cos" | "oss") {
    const connection = connections.find(item => item.provider === value);
    if (!connection) return;
    setProvider(value); accept(connection); setError(""); setNotice(""); setConflict(false); setPendingProvider(null);
  }
  async function act(test: boolean) {
    if (!config) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await request<Storage & { success?: boolean }>("/site/admin/storage" + (test ? "/test" : ""),
        json(test ? { provider, version: config.version } : { ...form, provider, version: config.version }, test ? "POST" : "PUT"));
      accept(result); setConflict(false);
      setConnections(previous => previous.map(item => item.provider === provider ? result : item));
      if (result.success === false) setError(result.test_result ?? "连接测试失败");
      else setNotice(test ? "连接成功" : "已保存");
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
      setConflict(e instanceof ApiError && e.status === 409);
    } finally { setBusy(false); }
  }
  return <>
    <Tabs.Root value={provider} onValueChange={value => {
      if (busy) return;
      if (dirty) setPendingProvider(value as "cos" | "oss"); else selectProvider(value as "cos" | "oss");
    }}><Tabs.List className="model-detail-tabs" aria-label="存储供应商">
      <Tabs.Trigger value="cos">腾讯云 COS</Tabs.Trigger><Tabs.Trigger value="oss">阿里云 OSS</Tabs.Trigger>
    </Tabs.List></Tabs.Root>
    {pendingProvider && <div className="admin-inline-confirm" role="alert"><span>有尚未保存的修改，要放弃吗？</span>
      <button className="secondary" onClick={() => setPendingProvider(null)}>继续编辑</button>
      <button className="danger" onClick={() => selectProvider(pendingProvider)}>放弃修改</button></div>}
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {config && <>
        <p className="model-default"><HardDrive size={15} />{config.active ? "当前主存储" : "未启用"}
          {config.tested ? " · 连接已验证" : ""}</p>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm"><span>配置可能已更新。重新加载会放弃本页修改。</span>
          <button className="secondary" disabled={busy} onClick={() => { setConflict(false); void resource.reload(); }}>重新加载</button></p>}
        <form className="provider-form model-detail-form" onSubmit={e => { e.preventDefault(); void act(false); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section"><h2>存储位置</h2>
              <div className="provider-form-grid">
                <label>存储桶名称<input required disabled={config.active} value={form.bucket} onChange={e => setForm({ ...form, bucket: e.target.value })} /></label>
                <label>地域<input required disabled={config.active} value={form.region} onChange={e => setForm({ ...form, region: e.target.value })} /></label>
              </div>
            </section>
            <section className="model-form-section"><h2>访问凭据</h2>
              <div className="provider-form-grid">
                <label><span className="provider-key-label">{provider === "oss" ? "AccessKey ID" : "SecretId"}<small>{config.secret_id_masked || "尚未配置"}</small></span>
                  <input type="password" autoComplete="new-password" disabled={form.clear_credentials} value={form.secret_id} onChange={e => setForm({ ...form, secret_id: e.target.value })} placeholder={config.has_secret_id ? "留空保留" : "输入专用凭据"} /></label>
                <label><span className="provider-key-label">{provider === "oss" ? "AccessKey Secret" : "SecretKey"}<small>{config.has_secret_key ? "已加密保存" : "尚未配置"}</small></span>
                  <input type="password" autoComplete="new-password" disabled={form.clear_credentials} value={form.secret_key} onChange={e => setForm({ ...form, secret_key: e.target.value })} placeholder={config.has_secret_key ? "留空保留" : "输入专用凭据"} /></label>
                {!config.active && <label className="provider-enabled provider-form-wide"><input type="checkbox" checked={form.clear_credentials} onChange={e => setForm({ ...form, clear_credentials: e.target.checked })} />清除已保存的凭据</label>}
              </div>
            </section>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <div className="admin-actions"><button className="secondary" type="button" disabled={busy || dirty || !config.has_secret_key} onClick={() => void act(true)}><PlugZap size={16} />测试连接</button>
                <button className="primary" disabled={busy || !dirty}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存配置</button></div>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
  </>;
}
