"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Building2, Check, Copy, Search } from "lucide-react";
import { Dialog } from "../../components/ui/dialog";
import { Tooltip } from "../../components/ui/tooltip";
import { AdminLoad, Pagination, useAdminResource, type Page } from "./admin-ui";
import type { AdminWorkspace } from "./workspaces/page";
import { useAdminChanges } from "./admin-changes";

const Changes = createContext({ setDirty: (_value: boolean) => {}, setBusy: (_value: boolean) => {} });

export function useWorkspaceChanges(dirty: boolean, busy: boolean) {
  useAdminChanges(dirty, busy);
  const { setDirty, setBusy } = useContext(Changes);
  useEffect(() => { setDirty(dirty); }, [dirty, setDirty]);
  useEffect(() => { setBusy(busy); }, [busy, setBusy]);
  useEffect(() => () => { setDirty(false); setBusy(false); }, [setDirty, setBusy]);
}

export function WorkspaceTarget({ children }: { children: (workspaceId: string) => ReactNode }) {
  const params = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const id = params.get("workspace");
  const [picker, setPicker] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  useEffect(() => { setPicker(false); setConfirm(false); setSelected(null); }, [id]);
  function select(next: string) {
    if (busy) return;
    const query = new URLSearchParams(params.toString());
    query.set("workspace", next);
    setPicker(false); setConfirm(false); setDirty(false);
    router.push(`${pathname}?${query}`, { scroll: false });
  }
  function apply() {
    if (!selected) return;
    if (selected === id) { setPicker(false); return; }
    if (dirty) setConfirm(true);
    else select(selected);
  }
  return <Changes.Provider value={{ setDirty, setBusy }}>
    {id ? <TargetContent key={id} id={id} busy={busy} change={() => { setSelected(id); setConfirm(false); setPicker(true); }}>{children(id)}</TargetContent>
      : <WorkspacePicker select={select} busy={busy} currentId={null} />}
    {picker && <Dialog title="更换管理工作区" onClose={() => setPicker(false)} restoreFocusSelector="[data-workspace-target]" wide>
      <p className="pricing-description">仅更换后台管理对象，不切换当前登录工作区。</p>
      <WorkspacePicker select={next => { setSelected(next); setConfirm(false); }} busy={busy} currentId={selected} radio />
      {confirm && <div className="admin-inline-confirm" role="alert">
        <span>当前倍率尚未保存。放弃修改并更换工作区？</span>
        <button className="secondary" onClick={() => setConfirm(false)}>继续选择</button>
        <button className="danger" onClick={() => selected && select(selected)}>放弃修改并更换</button>
      </div>}
      <div className="provider-form-actions target-picker-actions">
        <button className="secondary" onClick={() => setPicker(false)}>取消</button>
        <button className="primary" disabled={!selected || busy || confirm} onClick={apply}>确认更换</button>
      </div>
    </Dialog>}
  </Changes.Provider>;
}

function TargetContent({ id, children, busy, change }: { id: string; children: ReactNode; busy: boolean; change: () => void }) {
  const { data, loading, error, reload } = useAdminResource<AdminWorkspace>(`/site/admin/workspaces/${encodeURIComponent(id)}`);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  return <>
    <div className="admin-target-context">
      <Building2 size={20} aria-hidden="true" />
      <div><span className="billing-muted">管理工作区</span><strong>{data?.name ?? (error ? "工作区加载失败" : "加载工作区…")}</strong>
        <div className="admin-target-id"><code>{id}</code><Tooltip content={copied ? "已复制" : "复制工作区 ID"}>
          <button className="icon-button" aria-label="复制工作区 ID" onClick={async () => {
            try { await navigator.clipboard.writeText(id); setCopied(true); setCopyError(""); }
            catch { setCopyError("复制失败，请手动复制工作区 ID"); }
          }}>{copied ? <Check size={14} /> : <Copy size={14} />}</button>
        </Tooltip></div>
      </div>
      <button className="secondary" data-workspace-target disabled={busy} onClick={change}>更换</button>
    </div>
    {copyError && <p role="status" className="billing-muted">{copyError}</p>}
    <AdminLoad loading={loading} error={error} retry={reload}>{data && children}</AdminLoad>
  </>;
}

function WorkspacePicker({ select, busy, currentId, radio = false }: {
  select: (id: string) => void; busy: boolean; currentId: string | null; radio?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const { data, loading, error, reload } = useAdminResource<Page<AdminWorkspace>>(`/site/admin/workspaces?q=${encodeURIComponent(query)}&page=${page}&page_size=10`);
  return <section className="admin-workspace-picker" aria-label="选择管理工作区">
    <form className="admin-filters" onSubmit={event => {
      event.preventDefault(); setQuery(String(new FormData(event.currentTarget).get("q") ?? "")); setPage(1);
    }}>
      <label>搜索工作区<input name="q" placeholder="名称或 ID" maxLength={200} /></label>
      <button className="secondary"><Search size={16} />查询</button>
    </form>
    <AdminLoad loading={loading} error={error} retry={reload}>
      {data && <><div className="admin-workspace-options">
        {data.items.map(item => radio
          ? <label className="workspace-option" key={item.id}><input type="radio" name="target-workspace" checked={item.id === currentId} disabled={busy} onChange={() => select(item.id)} /><span><strong>{item.name}</strong><small>{item.id}</small></span></label>
          : <div key={item.id}><span><strong>{item.name}</strong><small>{item.id}</small></span><button className="secondary" disabled={busy} onClick={() => select(item.id)}>管理计费</button></div>)}
      </div>{!data.items.length && <p className="admin-empty">没有符合条件的工作区</p>}
      <Pagination total={data.total} page={data.page} pageSize={data.page_size} change={setPage} /></>}
    </AdminLoad>
  </section>;
}
