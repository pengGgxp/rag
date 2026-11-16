"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Globe, Loader2, Save } from "lucide-react";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../admin-ui";
import { useAdminChanges } from "../admin-changes";
import { PolicyTable } from "../../policy-table";
import { policyPayload, type Policy } from "../../model-types";
import { Dialog } from "../../../components/ui/dialog";
import { formatCredits } from "../../credit-format";
import "../../billing.css";

type Settings = { version: number; enabled: boolean; has_api_key: boolean; timeout_seconds: number;
  result_count: number; policies: Policy[] };
type TestResult = { completed: boolean; credits: number; result_count?: number; error?: string };

export default function SearchSettingsPage() {
  const resource = useAdminResource<Settings>("/site/admin/search");
  const [form, setForm] = useState<Settings | null>(null);
  const [original, setOriginal] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [clear, setClear] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [confirmTest, setConfirmTest] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState("");
  const testId = useRef<string | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(original) || !!key || clear;
  useAdminChanges(dirty, busy);
  function accept(value: Settings) {
    setForm(value); setOriginal(value); setKey(""); setClear(false); testId.current = null;
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  async function save() {
    if (!form) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const { has_api_key: _, ...settings } = form;
      accept(await request<Settings>("/site/admin/search", json({ ...settings,
        policies: form.policies.map(policyPayload), api_key: key || null, clear_api_key: clear }, "PUT")));
      setConflict(false); setNotice("联网搜索设置已保存");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请重试");
      setConflict(reason instanceof ApiError && reason.status === 409);
    } finally { setBusy(false); }
  }
  async function test() {
    if (!form) return;
    setBusy(true); setError(""); setNotice("");
    testId.current ??= crypto.randomUUID();
    try {
      const result = await request<TestResult>("/site/admin/search/test", json({ version: form.version, request_id: testId.current }));
      setNotice(result.completed ? `搜索成功 · 本次消费 ${formatCredits(result.credits)} 积分` : result.error || "未找到结果，本次不收取工具费用");
      testId.current = null;
    } catch (reason) { setError(reason instanceof Error ? reason.message : "测试失败，请重试"); }
    finally { setBusy(false); setConfirmTest(false); }
  }
  return <>
    <AdminHeading title="联网搜索" />
    <AdminLoad loading={resource.loading} error={resource.error} retry={resource.reload}>
      {form && <>
        {notice && <p className="billing-notice" role="status"><Check size={16} />{notice}</p>}
        {error && <div className="admin-error" role="alert">{error}</div>}
        {conflict && <p className="admin-inline-confirm"><span>重新加载会放弃本页修改。</span><button className="secondary" disabled={busy} onClick={() => { setConflict(false); setError(""); void resource.reload(); }}>放弃修改并重新加载</button></p>}
        <form className="provider-form model-detail-form search-settings-form" onSubmit={event => { event.preventDefault(); void save(); }}>
          <fieldset className="provider-form-fields" disabled={busy}>
            <section className="model-form-section"><h2>服务配置</h2>
              <div className="provider-form-grid">
                <label className="provider-enabled provider-form-wide"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用联网搜索</label>
                <label className="provider-form-wide">DeepSeek API Key<input type="password" autoComplete="new-password" value={key} disabled={clear} placeholder={clear ? "保存后清除" : form.has_api_key ? "已配置，留空保留" : "填写独立搜索密钥"} onChange={e => setKey(e.target.value)} /></label>
                {form.has_api_key && <div className="provider-form-wide"><button type="button" className="secondary" onClick={() => clear ? setClear(false) : setConfirmClear(true)}>{clear ? "撤销清除" : "清除密钥"}</button></div>}
                {([['timeout_seconds', '超时（秒）', 5, 60], ['result_count', '每次搜索结果数', 1, 10]] as const).map(([field, label, min, max]) => <label key={field}>{label}<input required type="number" min={min} max={max} step={1} value={form[field]} onChange={e => setForm({ ...form, [field]: Number(e.target.value) })} /></label>)}
              </div>
            </section>
            <section className="model-form-section"><h2>工具基准价</h2>
              <PolicyTable name="联网工具" policies={form.policies} busy={busy} requirePrices={form.enabled}
                update={(policy, patch) => setForm({ ...form, policies: form.policies.map(item => item.capability === policy.capability ? { ...item, ...patch } : item) })} />
              <p className="site-setting-note">仅有效搜索和读取正文计费，按工作区倍率计算。保存后的价格用于新请求。</p>
            </section>
            <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : ""}</span>
              <button type="button" className="secondary" disabled={busy || dirty || !form.enabled || conflict} onClick={() => setConfirmTest(true)}><Globe size={16} />测试搜索</button>
              <button className="primary" disabled={busy || !dirty || conflict}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存设置</button>
            </div>
          </fieldset>
        </form>
      </>}
    </AdminLoad>
    {confirmClear && <Dialog title="清除搜索密钥" onClose={() => setConfirmClear(false)}><p>保存后将清除密钥并停用联网搜索。</p><div className="modal-actions"><button className="secondary" onClick={() => setConfirmClear(false)}>取消</button><button className="primary" onClick={() => { setClear(true); setKey(""); if (form) setForm({ ...form, enabled: false }); setConfirmClear(false); }}>确认清除</button></div></Dialog>}
    {confirmTest && <Dialog title="测试联网搜索" busy={busy} onClose={() => setConfirmTest(false)}><p>使用已保存配置搜索一次，不自动读取网页。成功后按你当前工作区的价格和倍率扣除积分。</p><div className="modal-actions"><button className="secondary" disabled={busy} onClick={() => setConfirmTest(false)}>取消</button><button className="primary" disabled={busy} onClick={() => void test()}>{busy && <Loader2 size={16} className="spin" />}确认测试</button></div></Dialog>}
  </>;
}
