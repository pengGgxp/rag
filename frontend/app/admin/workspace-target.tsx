"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Building2, Search } from "lucide-react";
import { AdminLoad, Pagination, useAdminResource, type Page } from "./admin-ui";
import type { AdminWorkspace } from "./workspaces/page";

const Changes = createContext({ setDirty: (_value: boolean) => {}, setBusy: (_value: boolean) => {} });

export function useWorkspaceChanges(dirty: boolean, busy: boolean) {
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
  const [picker, setPicker] = useState(!id);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function select(next: string) {
    if (busy) return;
    if (dirty && next !== id) { setPending(next); return; }
    setPicker(false);
    router.push(`${pathname}?workspace=${encodeURIComponent(next)}`, { scroll: false });
  }
  return <Changes.Provider value={{ setDirty, setBusy }}>
    <div className="admin-target-toolbar">
      <span><Building2 size={18} />目标工作区</span>
      {id && <button className="secondary" disabled={busy} onClick={() => setPicker(value => !value)}>{picker ? "收起选择" : "更换工作区"}</button>}
    </div>
    {pending && <div className="admin-inline-confirm" role="alert">
      <span>有尚未保存的修改，要放弃并切换工作区吗？</span>
      <button className="secondary" onClick={() => setPending(null)}>继续编辑</button>
      <button className="danger" onClick={() => { const next = pending; setPending(null); setDirty(false); setPicker(false); router.push(`${pathname}?workspace=${encodeURIComponent(next)}`); }}>放弃修改</button>
    </div>}
    {(!id || picker) && <WorkspacePicker select={select} busy={busy} currentId={id} />}
    {id ? <TargetContent key={id} id={id}>{children(id)}</TargetContent> : <p className="admin-empty">请选择要管理的工作区</p>}
  </Changes.Provider>;
}

function TargetContent({ id, children }: { id: string; children: ReactNode }) {
  const { data, loading, error, reload } = useAdminResource<AdminWorkspace>(`/site/admin/workspaces/${encodeURIComponent(id)}`);
  return <AdminLoad loading={loading} error={error} retry={reload}>
    {data && <><div className="admin-target-name"><strong>{data.name}</strong><small>{data.id}</small></div>{children}</>}
  </AdminLoad>;
}

function WorkspacePicker({ select, busy, currentId }: { select: (id: string) => void; busy: boolean; currentId: string | null }) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const { data, loading, error, reload } = useAdminResource<Page<AdminWorkspace>>(`/site/admin/workspaces?q=${encodeURIComponent(query)}&page=${page}&page_size=10`);
  return <section className="admin-workspace-picker" aria-label="选择目标工作区">
    <form className="admin-filters" onSubmit={event => {
      event.preventDefault(); setQuery(String(new FormData(event.currentTarget).get("q") ?? "")); setPage(1);
    }}>
      <label>搜索工作区<input name="q" placeholder="名称或 ID" maxLength={200} /></label>
      <button className="secondary"><Search size={16} />查询</button>
    </form>
    <AdminLoad loading={loading} error={error} retry={reload}>
      {data && <><div className="admin-workspace-options">
        {data.items.map(item => <div key={item.id}><span><strong>{item.name}</strong><small>{item.id}</small></span><button className="secondary" disabled={busy} onClick={() => select(item.id)}>{item.id === currentId ? "已选择" : "选择"}</button></div>)}
      </div>{!data.items.length && <p className="admin-empty">没有符合条件的工作区</p>}
      <Pagination total={data.total} page={data.page} pageSize={data.page_size} change={setPage} /></>}
    </AdminLoad>
  </section>;
}
