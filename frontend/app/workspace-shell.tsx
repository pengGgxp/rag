"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Dialog } from "../components/ui/dialog";
import { WorkspaceSidebar } from "./workspace-sidebar";
import { useUiStore } from "./ui-store";
import type { WorkspaceScreen } from "./workspace-types";
import { usePathname } from "next/navigation";
import { SiteAnnouncementProvider } from "./site-announcement";
import { SuggestionProvider } from "./suggestion";

const NavigationSlot = createContext<HTMLDivElement | null>(null);

// Business modules own their navigation state; only its DOM is placed in the shared sidebar.
export function WorkspaceNavigation({ children }: { children: ReactNode }) {
  const target = useContext(NavigationSlot);
  return target ? createPortal(children, target) : null;
}

export function WorkspaceShell({
  screen,
  children,
}: {
  screen: WorkspaceScreen;
  children: ReactNode;
}) {
  const sidebarOpen = useUiStore((state) => state.sidebarOpen);
  const setSidebarOpen = useUiStore((state) => state.setSidebarOpen);
  const [mobile, setMobile] = useState(false);
  const [target, setTarget] = useState<HTMLDivElement | null>(null);
  const pathname = usePathname();
  useEffect(() => { setSidebarOpen(false); }, [pathname, setSidebarOpen]);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 700px)");
    const update = () => { setMobile(query.matches); setSidebarOpen(false); };
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, [setSidebarOpen]);
  const navigation = <WorkspaceSidebar screen={screen} onNavigate={() => setSidebarOpen(false)}>
    <div className="workspace-context-navigation" ref={setTarget} />
  </WorkspaceSidebar>;
  return <SiteAnnouncementProvider><SuggestionProvider><NavigationSlot.Provider value={target}>
    <div className="workspace">
      {!mobile && <aside className="sidebar" aria-label="工作空间导航">{navigation}</aside>}
      <main className="main">{children}</main>
    </div>
    {mobile && sidebarOpen && <Dialog title="工作空间导航" sidePanel
      onClose={() => setSidebarOpen(false)} restoreFocusSelector="[data-workspace-menu]">
      <aside className="sidebar" aria-label="工作空间导航">{navigation}</aside>
    </Dialog>}
  </NavigationSlot.Provider></SuggestionProvider></SiteAnnouncementProvider>;
}
