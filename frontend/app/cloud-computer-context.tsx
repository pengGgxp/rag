"use client";

import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { request } from "./api";
import { CloudComputerStore, type CloudScope } from "./cloud-computer-state";

const CloudContext = createContext<CloudComputerStore | null>(null);

export function CloudComputerProvider({ scope, children }: { scope: CloudScope; children: ReactNode }) {
  const { userId, workspaceId, conversationId, previewId } = scope;
  const store = useMemo(() => new CloudComputerStore({ userId, workspaceId, conversationId, previewId }, request),
    [userId, workspaceId, conversationId, previewId]);
  useEffect(() => {
    store.activate();
    const refresh = () => { if (!document.hidden) void store.refresh(); };
    refresh();
    const timer = setInterval(refresh, 5000);
    const resume = () => { if (!document.hidden) void store.refresh(true); };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("rag-session-expired", store.revoke);
    window.addEventListener("rag-session-ended", store.revoke);
    return () => {
      clearInterval(timer); store.dispose();
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("rag-session-expired", store.revoke);
      window.removeEventListener("rag-session-ended", store.revoke);
    };
  }, [store]);
  return <CloudContext.Provider value={store}>{children}</CloudContext.Provider>;
}

export function useCloudComputer() {
  const store = useContext(CloudContext);
  if (!store) throw new Error("Missing cloud computer scope");
  return { store, state: useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot) };
}
