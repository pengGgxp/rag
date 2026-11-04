"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { BookOpen, BookOpenCheck, Files, MessageSquare, Puzzle } from "lucide-react";
import { AccountButton, useAuth } from "./auth";
import { request } from "./api";
import type { WorkspaceScreen } from "./workspace-types";
import { UploadTaskEntry } from "./document-uploads";
import { SiteAnnouncementEntry } from "./site-announcement";
import { SuggestionEntry } from "./suggestion";

const destinations = [
  { mode: "catalog", href: "/", label: "知识库", icon: BookOpen },
  { mode: "chat", href: "/?chat=1", label: "知识问答", icon: MessageSquare },
  { mode: "extensions", href: "/extensions", label: "扩展", icon: Puzzle },
  { mode: "files", href: "/resources", label: "资源库", icon: Files },
] as const;

export function WorkspaceSidebar({ screen, children, onNavigate }: {
  screen: WorkspaceScreen;
  children: ReactNode;
  onNavigate: () => void;
}) {
  const mode = screen === "knowledge" ? "catalog" : screen;
  const { session } = useAuth();
  const [workbenchAvailable, setWorkbenchAvailable] = useState(false);
  useEffect(() => {
    let live = true;
    setWorkbenchAvailable(false);
    const refresh = () => { void request<{ available: boolean }>("/question-workbench")
      .then(value => { if (live) setWorkbenchAvailable(value.available); })
      .catch(() => { if (live) setWorkbenchAvailable(false); }); };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { live = false; window.removeEventListener("focus", refresh); };
  }, [session.user.id, session.workspace_id]);
  return <>
    <Link className="brand" href="/" onClick={onNavigate}>
      <span className="brand-mark"><img src="/icon.svg" alt="" /></span><span>知库</span>
    </Link>
    <nav className="primary-navigation" aria-label="产品导航">
      {destinations.map(({ mode: itemMode, href, label, icon: Icon }) => (
        <Link key={itemMode} href={href} scroll={false} onClick={onNavigate}
          className="navigation-item"
          aria-current={mode === itemMode ? "page" : undefined}>
          <Icon size={18} />{label}
        </Link>
      ))}
      {workbenchAvailable && <Link className="navigation-item" href="/question-workbench"
        target="_blank" rel="noreferrer" onClick={onNavigate}>
        <BookOpenCheck size={18} />题目工作台
      </Link>}
    </nav>
    {children}
    <div className="sidebar-footer"><SuggestionEntry onOpen={onNavigate} /><SiteAnnouncementEntry onOpen={onNavigate} /><UploadTaskEntry onOpen={onNavigate} /><AccountButton /></div>
  </>;
}
