"use client";
import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Search } from "lucide-react";
import { Dialog } from "../../../components/ui/dialog";
import { ApiError, request } from "../../api";
import { AdminHeading, AdminLoad, date, json, Pagination, type Page, useAdminResource } from "../admin-ui";
import "../platform-content.css";

const categories: Record<string, string> = { feature: "功能建议", problem: "使用问题", other: "其他" };
const statuses: Record<string, string> = { pending: "待处理", processing: "处理中", resolved: "已处理" };
type Suggestion = { id: string; category: string; summary: string; content: string; status: string; version: number; user_name: string; workspace_name: string; created_at: string; handled_at: string | null; handled_by: string | null };

export default function FeedbackPage() {
  const params = useSearchParams();
  const router = useRouter();
  const resource = useAdminResource<Page<Suggestion>>(`/site/admin/suggestions?${params}`);
  const [selected, setSelected] = useState<string | null>(null);
  return <>
    <AdminHeading title="意见反馈" />
    <form key={params.toString()} className="admin-filters" onSubmit={event => {
      event.preventDefault(); const data = new FormData(event.currentTarget); const next = new URLSearchParams();
      for (const [key, value] of data) if (value && value !== "all") next.set(key, String(value));
      router.push(`/admin/feedback?${next}`);
    }}>
      <label>搜索<input name="q" aria-label="搜索反馈" placeholder="反馈内容" maxLength={200} defaultValue={params.get("q") ?? ""} /></label>
      <label>类型<select name="category" defaultValue={params.get("category") ?? "all"}><option value="all">全部类型</option>{Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>状态<select name="status" defaultValue={params.get("status") ?? "all"}><option value="all">全部状态</option>{Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <button className="secondary" type="submit"><Search size={16} />查询</button>
    </form>
    <AdminLoad {...resource} retry={resource.reload}>
      <div className="managed-list" aria-label="意见反馈列表">{resource.data?.items.map(item => <button type="button" className="managed-row suggestion-row" key={item.id} data-feedback-id={item.id} onClick={() => setSelected(item.id)}>
        <div className="managed-main"><strong>{categories[item.category]}</strong><p>{item.summary}</p><small>{item.user_name} · {item.workspace_name}</small></div>
        <span className="admin-status">{statuses[item.status]}</span><time>{date(item.created_at)}</time>
      </button>)}</div>
      {resource.data && !resource.data.items.length && <p className="admin-empty">暂无符合条件的反馈</p>}
      {resource.data && <Pagination total={resource.data.total} page={resource.data.page} pageSize={resource.data.page_size} change={page => {
        const next = new URLSearchParams(params); next.set("page", String(page)); router.push(`/admin/feedback?${next}`);
      }} />}
    </AdminLoad>
    {selected && <FeedbackDetail key={selected} id={selected} onClose={() => setSelected(null)} onSaved={() => void resource.reload()} />}
  </>;
}

function FeedbackDetail({ id, onClose, onSaved }: { id: string; onClose: () => void; onSaved: () => void }) {
  const resource = useAdminResource<Suggestion>(`/site/admin/suggestions/${id}`);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [saved, setSaved] = useState<Suggestion | null>(null);
  const value = saved ?? resource.data;
  const dirty = !!status && status !== value?.status;
  return <Dialog title="反馈详情" busy={busy} onClose={() => dirty ? setDiscard(true) : onClose()} restoreFocusSelector={`[data-feedback-id="${id}"]`}>
    <div className="managed-dialog-body"><AdminLoad {...resource} retry={resource.reload}>
      {value && <>
        <div className="suggestion-meta"><span>{categories[value.category]}</span><span>{value.user_name} · {value.workspace_name}</span><time>{date(value.created_at)}</time></div>
        <div className="suggestion-body">{value.content}</div>
        <form className="suggestion-form" onSubmit={async event => {
          event.preventDefault(); setBusy(true); setError("");
          try { setSaved(await request<Suggestion>(`/site/admin/suggestions/${id}`, json({ version: value.version, status: status || value.status }, "PUT"))); setStatus(""); onSaved(); }
          catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败，请重试"); setConflict(reason instanceof ApiError && reason.status === 409); }
          finally { setBusy(false); }
        }}>
          <label>处理状态<select disabled={busy} value={status || value.status} onChange={event => setStatus(event.target.value)}>{Object.entries(statuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          {value.handled_at && <small>最近由 {value.handled_by} 处理于 {date(value.handled_at)}</small>}
          {error && <p className="form-error" role="alert">{error}</p>}
          {conflict && <button className="secondary" type="button" onClick={() => { setSaved(null); setStatus(""); setConflict(false); setError(""); void resource.reload(); }}>放弃修改并重新加载</button>}
          {discard && <div className="admin-inline-confirm" role="alert"><span>放弃尚未保存的状态修改？</span><button className="secondary" type="button" onClick={() => setDiscard(false)}>继续处理</button><button className="danger" type="button" onClick={onClose}>放弃修改</button></div>}
          <div className="managed-actions"><button className="primary" type="submit" disabled={busy || conflict || !dirty}>{busy ? "正在保存" : "保存状态"}</button></div>
        </form>
      </>}
    </AdminLoad></div>
  </Dialog>;
}
