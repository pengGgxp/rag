"use client";

import { useEffect, useState } from "react";
import { Save, Loader2 } from "lucide-react";
import { request } from "./api";
import { AdminLoad, json, useAdminResource } from "./admin/admin-ui";
import "./billing.css";

type Selection = { version: number; provider_id: string | null;
  providers: { id: string; name: string; model: string; available: boolean; enabled: boolean }[] };
type Props = { endpoint?: string; editable: boolean; onDirty: (value: boolean) => void; onBusy?: (value: boolean) => void };

// Shared selection form; host layouts own navigation and unsaved-change protection.
export function ModelSelectionPanel({ endpoint = "/settings/models", editable, onDirty, onBusy }: Props) {
  const { data, loading, error, reload } = useAdminResource<Selection>(endpoint);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { if (data) setSelected(data.provider_id ?? ""); }, [data]);
  const dirty = Boolean(data && selected !== (data.provider_id ?? ""));
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { onBusy?.(busy); return () => onBusy?.(false); }, [busy, onBusy]);
  return <section className="pricing-editor" aria-label="工作区模型选择">
    {failure && <div role="alert" className="admin-error"><span>{failure}</span><button className="secondary" onClick={() => void reload()}>重新加载</button></div>}
    {notice && <p role="status" className="billing-notice">{notice}</p>}
    <AdminLoad loading={loading} error={error} retry={reload}>{data && <form onSubmit={async e => {
      e.preventDefault(); setBusy(true); setFailure(""); setNotice("");
      try { await request(endpoint, json({ provider_id: selected, version: data.version }, "PUT"));
        await reload(); setNotice("模型已更新，将用于新的请求");
      } catch (error) { setFailure(error instanceof Error ? error.message : "保存失败"); }
      finally { setBusy(false); }
    }}>
      <div className="provider-form-grid"><label className="provider-form-wide">当前模型
        <select aria-label="当前模型" value={selected} disabled={!editable || busy} onChange={e => setSelected(e.target.value)}>
          <option value="" disabled>请选择模型</option>
          {data.providers.map(p => <option key={p.id} value={p.id} disabled={!p.available}>{p.name} · {p.model}{!p.available ? "（暂不可选）" : ""}</option>)}
        </select>
      </label></div>
      {!data.providers.some(p => p.available) && <p className="pricing-description">暂无可选模型，请联系站点管理员。</p>}
      {editable && <div className="pricing-footer"><span>{dirty ? "有未保存的修改" : "模型切换不影响正在进行的回答"}</span><button className="primary" disabled={busy || !dirty || !selected}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存选择</button></div>}
    </form>}</AdminLoad>
  </section>;
}
