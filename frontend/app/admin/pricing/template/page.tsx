"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, CircleAlert, Loader2, Save } from "lucide-react";
import { request } from "../../../api";
import { policyPayload, type Policy } from "../../../model-types";
import { PolicyTable } from "../../../policy-table";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../../admin-ui";
import { useAdminChanges } from "../../admin-changes";
import "../../../billing.css";

type Template = { version: number; configured: boolean; billing_enabled: boolean; policies: Policy[] };

export default function PricingTemplatePage() {
  const workspace = useSearchParams().get("workspace");
  const { data, loading, error, reload } = useAdminResource<Template>("/site/admin/pricing-template");
  const [form, setForm] = useState<Template | null>(null);
  const [original, setOriginal] = useState<Template | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { if (data) { setForm(data); setOriginal(data); } }, [data]);
  const dirty = JSON.stringify(form) !== JSON.stringify(original);
  const complete = Boolean(form?.policies.every(policy => policy.credits_per_unit !== null && Number.isInteger(policy.credits_per_unit) && policy.credits_per_unit >= 0 && Number.isInteger(policy.unit_size) && policy.unit_size > 0));
  useAdminChanges(dirty, busy);
  async function save() {
    if (!form) return;
    setBusy(true); setSaveError(""); setNotice("");
    try {
      const updated = await request<Template>("/site/admin/pricing-template", json({
        version: form.version, policies: form.policies.map(policyPayload),
      }, "PUT"));
      setForm(updated); setOriginal(updated); setNotice("模板已保存，仅用于之后创建的工作区");
    } catch (e) { setSaveError(e instanceof Error ? e.message : "保存失败，请重试"); }
    finally { setBusy(false); }
  }
  return <><AdminHeading title="新工作区价格模板"><Link className="secondary" href={`/admin/pricing${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`}><ArrowLeft size={16} />工作区价格</Link></AdminHeading>
    <section className="pricing-editor" aria-label="模型价格模板">
    <p className="pricing-description">新工作区使用此模板的初始价格，已有工作区不受影响。免费请明确填写 0。</p>
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {saveError && <div className="admin-error" role="alert"><span>{saveError}</span><button className="secondary" onClick={() => void reload()}>重新加载</button></div>}
    <AdminLoad loading={loading} error={error} retry={reload}>
      {form && <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className="pricing-summary">
          <span className="pricing-status" data-warning={!form.configured}>{form.configured ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}{form.configured ? `模板版本 ${form.version}` : "模板待配置"}</span>
          <span className="pricing-summary-detail">{form.billing_enabled ? "新结算已启用" : "新结算未启用"}</span>
        </div>
        <PolicyTable name="新工作区价格模板" policies={form.policies} busy={busy} update={(policy, patch) => setForm(value => value && ({ ...value,
          policies: value.policies.map(item => item.capability === policy.capability ? { ...item, ...patch } : item) }))} />
        <div className="pricing-footer"><span>{!complete ? "请补齐三项价格后保存" : dirty ? "有未保存的修改" : "当前模板已保存"}</span><button className="primary" disabled={busy || !dirty || !complete}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存模板</button></div>
      </form>}
    </AdminLoad>
    </section>
  </>;
}
