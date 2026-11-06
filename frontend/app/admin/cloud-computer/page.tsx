"use client";

import { useEffect, useState } from "react";
import { Check, Loader2, Plug, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import { Dialog } from "../../../components/ui/dialog";
import "../../billing.css";

type Settings = {
  version: number; enabled: boolean; has_api_key: boolean; region: string; template: string;
  specification: string; credits_per_minute: number | null; idle_seconds: number;
  max_seconds: number; concurrency: number; blocked_hosts: string[];
};

export default function CloudComputerSettings() {
  const resource = useAdminResource<Settings>("/site/admin/cloud-computer");
  const [form, setForm] = useState<Settings | null>(null);
  const [original, setOriginal] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [clear, setClear] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = JSON.stringify(form) !== JSON.stringify(original) || !!key || clear;
  useAdminChanges(dirty, busy);
  function accept(value: Settings) { setForm(value); setOriginal(value); setKey(""); setClear(false); }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function save() {
    if (!form) return;
    setBusy(true); setError(""); setNotice("");
    const { has_api_key: _, ...settings } = form;
    try {
      accept(await request<Settings>("/site/admin/cloud-computer", json({ ...settings, blocked_hosts: settings.blocked_hosts.map(host => host.trim()).filter(Boolean), api_key: key || null, clear_api_key: clear }, "PUT")));
      setConflict(false); setNotice("云电脑设置已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试");
      setConflict(cause instanceof ApiError && cause.status === 409);
    } finally { setBusy(false); }
  }
  async function check() {
    setBusy(true); setError(""); setNotice("");
    try {
      await request("/site/admin/cloud-computer/check", json({}));
      setNotice("连接成功，未启动收费环境；模板仍需实际验证");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "连接检查失败"); }
    finally { setBusy(false); }
  }
  return <>
    <AdminHeading title="云电脑" />
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {form && <>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <div className="admin-inline-confirm"><span>保留了本页修改，重新加载将放弃修改。</span><button className="secondary" disabled={busy} onClick={() => { setConflict(false); setError(""); void resource.reload(); }}>重新加载</button></div>}
        <form className="provider-form model-detail-form" onSubmit={event => { event.preventDefault(); void save(); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section"><h2>阿里云 Agent Sandbox</h2>
              <a href="https://sandbox.console.aliyun.com/" target="_blank" rel="noopener noreferrer">前往阿里云控制台</a>
              <div className="provider-form-grid">
                <label className="provider-enabled provider-form-wide"><input type="checkbox" checked={form.enabled} onChange={event => setForm({ ...form, enabled: event.target.checked })} />启用云电脑</label>
                <label>地域<input required={form.enabled} value={form.region} placeholder="如 cn-hangzhou" onChange={event => setForm({ ...form, region: event.target.value })} /></label>
                <label>模板 ID<input required={form.enabled} value={form.template} onChange={event => setForm({ ...form, template: event.target.value })} /></label>
                <label className="provider-form-wide">模板规格<input required={form.enabled} value={form.specification} placeholder="填写模板实际 CPU 和内存规格" onChange={event => setForm({ ...form, specification: event.target.value })} /></label>
                <label className="provider-form-wide">API Key<input type="password" autoComplete="new-password" value={key} disabled={clear} placeholder={clear ? "保存后清除" : form.has_api_key ? "已配置，留空保留" : "尚未配置"} onChange={event => setKey(event.target.value)} /></label>
                {form.has_api_key && <div className="provider-form-wide"><button type="button" data-cloud-clear className="secondary" onClick={() => clear ? setClear(false) : setConfirmClear(true)}>{clear ? "撤销清除" : "清除密钥"}</button></div>}
                <label className="provider-form-wide">禁止访问的业务域名或 IP<textarea rows={3} value={form.blocked_hosts.join("\n")} onChange={event => setForm({ ...form, blocked_hosts: event.target.value.split("\n") })} /></label>
              </div>
            </section>
            <section className="model-form-section"><h2>计费与运行</h2><div className="provider-form-grid">
              <label>每分钟积分<input type="number" min={0} max={2147483647} step="0.0001" required={form.enabled} value={form.credits_per_minute ?? ""} onChange={event => setForm({ ...form, credits_per_minute: event.target.value === "" ? null : Number(event.target.value) })} /></label>
              {([["idle_seconds", "闲置结束时间（秒）", 60, 1800], ["max_seconds", "最长运行时间（秒）", 300, 3600], ["concurrency", "同时运行上限", 1, 100]] as const).map(([field, label, min, max]) => <label key={field}>{label}<input type="number" required min={min} max={max} step={1} value={form[field]} onChange={event => setForm({ ...form, [field]: Number(event.target.value) })} /></label>)}
            </div><p className="site-setting-note">按存续秒数计费，等待期间计费。新价格用于新启动的云电脑。</p></section>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <button type="button" className="secondary" disabled={busy || dirty || !form.has_api_key} onClick={() => void check()}><Plug size={16} />检查连接</button>
              <button className="primary" disabled={busy || !dirty || conflict}>{busy ? <Loader2 className="spin" size={16} /> : <Save size={16} />}保存设置</button>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
    {confirmClear && <Dialog title="清除云电脑密钥" restoreFocusSelector="[data-cloud-clear]" onClose={() => setConfirmClear(false)}><p>保存后停止开放新环境。已启动的云电脑继续按原配置运行和结束。</p><div className="modal-actions"><button className="secondary" onClick={() => setConfirmClear(false)}>取消</button><button className="primary" onClick={() => { setClear(true); setKey(""); if (form) setForm({ ...form, enabled: false }); setConfirmClear(false); }}>确认清除</button></div></Dialog>}
  </>;
}
