"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

const Changes = createContext({ setDirty: (_value: boolean) => {}, setBusy: (_value: boolean) => {} });

export function useAdminChanges(dirty: boolean, busy: boolean) {
  const { setDirty, setBusy } = useContext(Changes);
  useEffect(() => { setDirty(dirty); }, [dirty, setDirty]);
  useEffect(() => { setBusy(busy); }, [busy, setBusy]);
  useEffect(() => () => { setDirty(false); setBusy(false); }, [setDirty, setBusy]);
}

export function AdminChangesBoundary({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  return <Changes.Provider value={{ setDirty, setBusy }}>
    <div className="admin-changes-boundary" onClickCapture={event => {
      const link = (event.target as HTMLElement).closest("a[href]") as HTMLAnchorElement | null;
      if (!link || link.target === "_blank" || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (link.origin !== window.location.origin || link.href === window.location.href) return;
      if (busy || dirty) { event.preventDefault(); event.stopPropagation(); if (!busy) setPending(link.pathname + link.search + link.hash); }
    }}>
      {pending && <div className="admin-inline-confirm admin-navigation-confirm" role="alert">
        <span>有尚未保存的修改，要放弃并离开吗？</span>
        <button className="secondary" onClick={() => setPending(null)}>继续编辑</button>
        <button className="danger" onClick={() => { const path = pending; setPending(null); setDirty(false); router.push(path); }}>放弃修改</button>
      </div>}
      {children}
    </div>
  </Changes.Provider>;
}
