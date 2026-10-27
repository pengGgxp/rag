"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowLeft, Loader2, Save } from "lucide-react";
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
  useAdminChanges(JSON.stringify(form) !== JSON.stringify(original), busy);
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
    <p className="billing-muted">新工作区创建时复制完整模板，后续独立管理。已有工作区与历史账单不受影响，免费请明确填写 0。</p>
    {notice && <p className="billing-notice" role="status">{notice}</p>}
    {saveError && <div className="admin-error" role="alert"><span>{saveError}</span><button className="secondary" onClick={() => void reload()}>重新加载</button></div>}
    <AdminLoad loading={loading} error={error} retry={reload}>
      {form && <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className="admin-target-toolbar"><span>{form.configured ? `模板版本 ${form.version}` : "模板待配置"}</span><span>{form.billing_enabled ? "请求合计结算已启用" : "请求合计结算未启用"}</span></div>
        <PolicyTable name="新工作区价格模板" policies={form.policies} busy={busy} update={(policy, patch) => setForm(value => value && ({ ...value,
          policies: value.policies.map(item => item.capability === policy.capability ? { ...item, ...patch } : item) }))} />
        <div className="admin-target-toolbar"><button className="primary" disabled={busy}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存完整模板</button></div>
      </form>}
    </AdminLoad>
  </>;
}
