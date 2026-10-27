"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2, Save } from "lucide-react";
import { request } from "../../../api";
import { policyPayload, type Policy } from "../../../model-types";
import { PolicyTable } from "../../../policy-table";
import { AdminHeading, AdminLoad, json, useAdminResource } from "../../admin-ui";
import { useAdminChanges } from "../../admin-changes";
import "../../../billing.css";

type Template = { version: number; configured: boolean; billing_enabled: boolean; shared_pricing_enabled: boolean; policies: Policy[] };

export default function PricingTemplatePage() {
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
        version: form.version, policies: form.policies.filter(p => p.credits_per_unit !== null).map(policyPayload),
      }, "PUT"));
      setForm(updated); setOriginal(updated); setNotice(updated.shared_pricing_enabled ? "平台基准价已保存，后续文档处理使用新价格；已有模型基准价不变" : "价格已保存，共享计费启用后文档基准价生效");
    } catch (e) { setSaveError(e instanceof Error ? e.message : "保存失败，请重试"); }
    finally { setBusy(false); }
  }
  return <><AdminHeading title="平台价格" />
    <section className="pricing-editor" aria-label="平台基准价与模型初始价">
    <p className="pricing-description">OCR 与 Embedding 基准价作用于后续文档处理；模型初始价仅初始化新模型配置，不覆盖已有模型。免费请明确填写 0。</p>
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {saveError && <div className="admin-error" role="alert"><span>{saveError}</span><button className="secondary" onClick={() => void reload()}>重新加载</button></div>}
    <AdminLoad loading={loading} error={error} retry={reload}>
      {form && <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className="pricing-summary">
          <span className="pricing-status" data-warning={!form.configured}>{form.configured ? <CheckCircle2 size={16} /> : <CircleAlert size={16} />}{form.configured ? `版本 ${form.version}` : "部分价格待配置"}</span>
          <span className="pricing-summary-detail">{form.shared_pricing_enabled ? "共享计费已启用" : "共享计费待启用"}</span>
        </div>
        {[{ name: "平台文档基准价", model: false }, { name: "新模型初始价", model: true }].map(group => <section className="pricing-group" key={group.name}>
          <div className="pricing-group-heading"><h2>{group.name}</h2></div>
          <PolicyTable name={group.name} requirePrices={false} policies={form.policies.filter(p => p.capability.startsWith("llm_") === group.model)} busy={busy} update={(policy, patch) => setForm(value => value && ({ ...value,
            policies: value.policies.map(item => item.capability === policy.capability ? { ...item, ...patch } : item) }))} />
        </section>)}
        <div className="pricing-footer"><span>{!complete ? "未填写的能力保留为待配置" : dirty ? "有未保存的修改" : "当前价格已保存"}</span><button className="primary" disabled={busy || !dirty}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存平台价格与初始价</button></div>
      </form>}
    </AdminLoad>
    </section>
  </>;
}
