"use client";

import { useEffect, useState } from "react";
import { Check, Gift, Loader2, Megaphone, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import "../../billing.css";

type Settings = {
  version: number; signup_credits: number; announcement_title: string;
  announcement_body: string; announcement_published: boolean;
};
type Draft = Omit<Settings, "version" | "signup_credits"> & { signup_credits: string };
const empty: Draft = { signup_credits: "1000", announcement_title: "", announcement_body: "", announcement_published: false };

export default function SiteSettingsPage() {
  const resource = useAdminResource<Settings>("/site/admin/settings");
  const [config, setConfig] = useState<Settings | null>(null);
  const [form, setForm] = useState<Draft>(empty);
  const [original, setOriginal] = useState<Draft>(empty);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  useAdminChanges(dirty, busy);
  function accept(value: Settings) {
    const next = { signup_credits: String(value.signup_credits), announcement_title: value.announcement_title,
      announcement_body: value.announcement_body, announcement_published: value.announcement_published };
    setConfig(value); setForm(next); setOriginal(next);
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function save() {
    if (!config) return;
    setBusy(true); setError(""); setNotice("");
    try {
      accept(await request<Settings>("/site/admin/settings",
        json({ ...form, signup_credits: Number(form.signup_credits), version: config.version }, "PUT")));
      setConflict(false); setNotice("站点设置已保存");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally { setBusy(false); }
  }
  return <>
    <AdminHeading title="站点设置" />
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {config && <>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm"><span>重新加载会放弃本页修改。</span>
          <button type="button" className="secondary" disabled={busy} onClick={() => {
            setConflict(false); setError(""); void resource.reload();
          }}>放弃修改并重新加载</button></p>}
        <form className="provider-form model-detail-form site-settings-form" onSubmit={event => {
          event.preventDefault(); void save();
        }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section">
              <h2><Gift size={17} />注册赠送</h2>
              <div className="provider-form-grid">
                <label>赠送积分<span className="policy-input"><input aria-label="赠送积分" required type="number" min={0} max={1000000} step={1}
                  value={form.signup_credits} onChange={event => setForm({ ...form, signup_credits: event.target.value })} /><span aria-hidden="true">积分</span></span></label>
              </div>
              <p className="site-setting-note">仅赠送给新注册账号，已有余额不变。设为 0 则不赠送。</p>
            </section>
            <section className="model-form-section">
              <h2><Megaphone size={17} />站点公告</h2>
              <div className="provider-form-grid">
                <label className="provider-enabled provider-form-wide"><input type="checkbox"
                  checked={form.announcement_published} onChange={event => setForm({ ...form, announcement_published: event.target.checked })} />发布到工作台</label>
                <label className="provider-form-wide">公告标题<input aria-label="公告标题" required={form.announcement_published} maxLength={120}
                  value={form.announcement_title} onChange={event => setForm({ ...form, announcement_title: event.target.value })} /></label>
                <label className="provider-form-wide">公告正文<textarea aria-label="公告正文" required={form.announcement_published} maxLength={10000} rows={8}
                  value={form.announcement_body} onChange={event => setForm({ ...form, announcement_body: event.target.value })} /></label>
              </div>
              <p className="site-setting-note">{form.announcement_published ? "保存后向所有已登录用户展示。" : "未发布，内容仅管理员可见。"}</p>
              {(form.announcement_title.trim() || form.announcement_body.trim()) && <details className="site-announcement-preview">
                <summary>预览公告</summary><h3>{form.announcement_title}</h3><div>{form.announcement_body}</div>
              </details>}
            </section>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <button type="submit" className="primary" disabled={busy || !dirty || conflict}>
                {busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存设置</button>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
  </>;
}
