"use client";

import { useEffect, useState } from "react";
import { Check, HardDrive, Loader2, PlugZap, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import "../../billing.css";

type Storage = {
  version: number; bucket: string; region: string; has_secret_id: boolean; has_secret_key: boolean;
  secret_id_masked: string; active: boolean; tested: boolean; test_result: string | null;
};
const initial = { bucket: "zk-1309923787", region: "ap-guangzhou", secret_id: "", secret_key: "", clear_credentials: false };
export default function StoragePage() {
  const resource = useAdminResource<Storage>("/site/admin/storage");
  const [config, setConfig] = useState<Storage | null>(null);
  const [form, setForm] = useState(initial);
  const [original, setOriginal] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  useAdminChanges(dirty, busy);
  function accept(value: Storage) {
    setConfig(value);
    const next = { ...initial, bucket: value.bucket, region: value.region };
    setForm(next); setOriginal(next);
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function act(test: boolean) {
    if (!config) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await request<Storage & { success?: boolean }>("/site/admin/storage" + (test ? "/test" : ""),
        json(test ? { version: config.version } : { ...form, version: config.version }, test ? "POST" : "PUT"));
      accept(result); setConflict(false);
      if (result.success === false) setError(result.test_result ?? "连接测试失败");
      else setNotice(test ? result.test_result ?? "验证通过" : "配置已保存，请测试连接");
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
      setConflict(e instanceof ApiError && e.status === 409);
    } finally { setBusy(false); }
  }
  return <>
    <AdminHeading title="文件存储"><span className="model-default"><HardDrive size={15} />腾讯云 COS</span></AdminHeading>
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {config && <>
        <div className="model-detail-summary"><span className="model-availability" data-warning={!config.active}>
          {config.active ? "正在使用" : config.tested ? "已验证，等待原文件迁移" : "待配置与验证"}</span><span>私有读写 · 单文件最大 200 MiB</span></div>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm"><span>配置可能已更新。重新加载会放弃本页修改。</span>
          <button className="secondary" disabled={busy} onClick={() => { setConflict(false); void resource.reload(); }}>重新加载</button></p>}
        <form className="provider-form model-detail-form" onSubmit={e => { e.preventDefault(); void act(false); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section"><h2>存储位置</h2>
              <p className="billing-muted">原文件保存在腾讯云。启用后不能直接更换桶或地域，避免历史文档失去来源。</p>
              <div className="provider-form-grid">
                <label>存储桶名称<input required disabled={config.active} value={form.bucket} onChange={e => setForm({ ...form, bucket: e.target.value })} /></label>
                <label>地域<input required disabled={config.active} value={form.region} onChange={e => setForm({ ...form, region: e.target.value })} /></label>
              </div>
            </section>
            <section className="model-form-section"><h2>访问凭据</h2>
              <div className="provider-form-grid">
                <label><span className="provider-key-label">SecretId<small>{config.secret_id_masked || "尚未配置"}</small></span>
                  <input type="password" autoComplete="new-password" disabled={form.clear_credentials} value={form.secret_id} onChange={e => setForm({ ...form, secret_id: e.target.value })} placeholder={config.has_secret_id ? "留空保留" : "输入专用凭据"} /></label>
                <label><span className="provider-key-label">SecretKey<small>{config.has_secret_key ? "已加密保存" : "尚未配置"}</small></span>
                  <input type="password" autoComplete="new-password" disabled={form.clear_credentials} value={form.secret_key} onChange={e => setForm({ ...form, secret_key: e.target.value })} placeholder={config.has_secret_key ? "留空保留" : "输入专用凭据"} /></label>
                {!config.active && <label className="provider-enabled provider-form-wide"><input type="checkbox" checked={form.clear_credentials} onChange={e => setForm({ ...form, clear_credentials: e.target.checked })} />清除已保存的凭据</label>}
              </div>
              <p className="billing-muted">仅使用此存储桶的最小权限子账号凭据。凭据不会回显；测试会创建临时对象，验证读取、分片及清理权限。</p>
            </section>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : `配置版本 ${config.version}`}</span>
              <div className="admin-actions"><button className="secondary" type="button" disabled={busy || dirty || !config.has_secret_key} onClick={() => void act(true)}><PlugZap size={16} />测试连接</button>
                <button className="primary" disabled={busy || !dirty}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存配置</button></div>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
  </>;
}
