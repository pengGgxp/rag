"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

const Changes = createContext({ setDirty: (_value: boolean) => {}, setBusy: (_value: boolean) => {} });

export function useAdminChanges(dirty: boolean, busy: boolean) {
  const { setDirty, setBusy } = useContext(Changes);
  useEffect(() => { setDirty(dirty); }, [dirty, setDirty]);
  useEffect(() => { setBusy(busy); }, [busy, setBusy]);
  useEffect(() => () => { setDirty(false); setBusy(false); }, [setDirty, setBusy]);
}

export function AdminChangesBoundary({ children, guardHistory = false }: { children: ReactNode; guardHistory?: boolean }) {
  const router = useRouter();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [pendingHistory, setPendingHistory] = useState<number | string | null>(null);
  const state = useRef({ dirty, busy });
  state.current = { dirty, busy };
  useEffect(() => {
    if (!guardHistory) return;
    type Navigation = EventTarget & { traverseTo: (key: string) => unknown };
    const navigation = (window as unknown as { navigation?: Navigation }).navigation;
    if (navigation) {
      let allowed = false;
      const navigate = (event: Event) => {
        const next = event as Event & { navigationType: string; destination: { key: string } };
        if (next.navigationType !== "traverse") return;
        if (allowed) { allowed = false; return; }
        if (!state.current.dirty && !state.current.busy || !event.cancelable) return;
        event.preventDefault();
        if (!state.current.busy) setPendingHistory(next.destination.key);
      };
      const proceed = () => { allowed = true; };
      navigation.addEventListener("navigate", navigate);
      window.addEventListener("rag-confirm-history", proceed);
      return () => {
        navigation.removeEventListener("navigate", navigate);
        window.removeEventListener("rag-confirm-history", proceed);
      };
    }
    const push = history.pushState.bind(history);
    const replace = history.replaceState.bind(history);
    let index = Number(history.state?.ragChangesIndex ?? 0);
    let restoring = false;
    let allowed = false;
    replace({ ...history.state, ragChangesIndex: index }, "");
    const wrappedPush: History["pushState"] = (data, unused, url) => {
      index += 1; push({ ...data, ragChangesIndex: index }, unused, url);
    };
    const wrappedReplace: History["replaceState"] = (data, unused, url) =>
      replace({ ...data, ragChangesIndex: index }, unused, url);
    history.pushState = wrappedPush;
    history.replaceState = wrappedReplace;
    const navigate = (event: PopStateEvent) => {
      const target = Number(event.state?.ragChangesIndex ?? index - 1);
      if (restoring) { restoring = false; event.stopImmediatePropagation(); return; }
      if (!allowed && (state.current.dirty || state.current.busy)) {
        const delta = target - index;
        if (!delta) return;
        event.stopImmediatePropagation();
        restoring = true;
        history.go(-delta);
        if (!state.current.busy) setPendingHistory(delta);
        return;
      }
      allowed = false; index = target;
    };
    const proceed = () => { allowed = true; };
    window.addEventListener("popstate", navigate, true);
    window.addEventListener("rag-confirm-history", proceed);
    return () => {
      window.removeEventListener("popstate", navigate, true);
      window.removeEventListener("rag-confirm-history", proceed);
      if (history.pushState === wrappedPush) history.pushState = push;
      if (history.replaceState === wrappedReplace) history.replaceState = replace;
    };
  }, [guardHistory]);
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
      {(pending || pendingHistory !== null) && <div className="admin-inline-confirm admin-navigation-confirm" role="alert">
        <span>有尚未保存的修改，要放弃并离开吗？</span>
        <button className="secondary" onClick={() => { setPending(null); setPendingHistory(null); }}>继续编辑</button>
        <button className="danger" onClick={() => {
          const path = pending, delta = pendingHistory;
          setPending(null); setPendingHistory(null); setDirty(false);
          if (delta !== null) {
            window.dispatchEvent(new Event("rag-confirm-history"));
            if (typeof delta === "string") {
              (window as unknown as { navigation: { traverseTo: (key: string) => unknown } }).navigation.traverseTo(delta);
            } else history.go(delta);
          }
          else if (path) router.push(path);
        }}>放弃修改</button>
      </div>}
      {children}
    </div>
  </Changes.Provider>;
}
