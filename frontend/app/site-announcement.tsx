"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Megaphone, X } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { request } from "./api";
import { useAuth } from "./auth";

type Announcement = { revision: number; title: string; body: string; updated_at: string | null };
type State = { announcement: Announcement | null; dismissed: boolean; dismiss: () => void; open: () => void };
const AnnouncementContext = createContext<State>({ announcement: null, dismissed: true, dismiss() {}, open() {} });

export function SiteAnnouncementProvider({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const [data, setData] = useState<{ userId: string; announcement: Announcement | null }>({ userId: "", announcement: null });
  const [dismissed, setDismissed] = useState(0);
  const [open, setOpen] = useState(false);
  const storageKey = `rag-announcement-dismissed:${session.user.id}`;
  const focusTarget = useRef<HTMLElement | null>(null);
  const announcement = data.userId === session.user.id ? data.announcement : null;
  useEffect(() => {
    let stopped = false;
    let running = false;
    const controller = new AbortController();
    setOpen(false);
    try { setDismissed(Number(localStorage.getItem(storageKey)) || 0); } catch { setDismissed(0); }
    const load = async () => {
      if (running || document.visibilityState === "hidden") return;
      running = true;
      try {
        const result = await request<{ announcement: Announcement | null }>("/site/announcement", { signal: controller.signal });
        if (!stopped) setData({ userId: session.user.id, announcement: result.announcement });
      } catch { /* Announcements must not block the workspace. */ }
      finally { running = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    window.addEventListener("focus", load);
    return () => { stopped = true; controller.abort(); window.clearInterval(timer); window.removeEventListener("focus", load); };
  }, [session.user.id, storageKey]);
  useEffect(() => { if (!announcement) setOpen(false); }, [announcement]);
  const dismiss = () => {
    if (!announcement) return;
    setDismissed(announcement.revision);
    try { localStorage.setItem(storageKey, String(announcement.revision)); } catch { /* Session-only dismissal still works. */ }
  };
  return <AnnouncementContext.Provider value={{ announcement, dismissed: dismissed === announcement?.revision,
    dismiss, open: () => {
      focusTarget.current?.removeAttribute("data-announcement-return");
      focusTarget.current = document.activeElement as HTMLElement | null;
      focusTarget.current?.setAttribute("data-announcement-return", "true");
      setOpen(true);
    } }}>
    {children}
    {open && announcement && <Dialog title={announcement.title} restoreFocusSelector="[data-announcement-return]"
      onClose={() => setOpen(false)}>
      <div className="site-announcement-content">{announcement.body}</div>
    </Dialog>}
  </AnnouncementContext.Provider>;
}

export function SiteAnnouncementBanner() {
  const state = useContext(AnnouncementContext);
  if (!state.announcement || state.dismissed) return null;
  return <aside className="site-announcement-banner" aria-label="站点公告">
    <Megaphone size={16} aria-hidden="true" />
    <button type="button" className="announcement-title" onClick={state.open} title={state.announcement.title}>
      <span>{state.announcement.title}</span><small>查看公告</small>
    </button>
    <Tooltip content="关闭公告"><button type="button" className="icon-button" aria-label="关闭公告" onClick={state.dismiss}><X size={16} /></button></Tooltip>
  </aside>;
}

export function SiteAnnouncementEntry({ onOpen }: { onOpen: () => void }) {
  const state = useContext(AnnouncementContext);
  if (!state.announcement) return null;
  return <button type="button" className="navigation-item site-announcement-entry" onClick={() => { state.open(); onOpen(); }}>
    <Megaphone size={18} />站点公告
  </button>;
}
