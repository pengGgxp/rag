"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { BookOpen, Layers3, MessageSquare, Puzzle } from "lucide-react";
import { AccountButton } from "./auth";
import type { WorkspaceScreen } from "./workspace-types";
import { UploadTaskEntry } from "./document-uploads";
import { SiteAnnouncementEntry } from "./site-announcement";
import { SuggestionEntry } from "./suggestion";

const destinations = [
  { mode: "catalog", href: "/", label: "知识库", icon: BookOpen },
  { mode: "chat", href: "/?chat=1", label: "知识问答", icon: MessageSquare },
  { mode: "extensions", href: "/extensions", label: "扩展", icon: Puzzle },
] as const;

export function WorkspaceSidebar({ screen, children, onNavigate }: {
  screen: WorkspaceScreen;
  children: ReactNode;
  onNavigate: () => void;
}) {
  const mode = screen === "knowledge" ? "catalog" : screen;
  return <>
    <Link className="brand" href="/" onClick={onNavigate}>
      <span className="brand-mark"><Layers3 size={23} /></span><span>知库</span>
    </Link>
    <nav className="primary-navigation" aria-label="产品导航">
      {destinations.map(({ mode: itemMode, href, label, icon: Icon }) => (
        <Link key={itemMode} href={href} scroll={false} onClick={onNavigate}
          className="navigation-item"
          aria-current={mode === itemMode ? "page" : undefined}>
          <Icon size={18} />{label}
        </Link>
      ))}
    </nav>
    {children}
    <div className="sidebar-footer"><SuggestionEntry onOpen={onNavigate} /><SiteAnnouncementEntry onOpen={onNavigate} /><UploadTaskEntry onOpen={onNavigate} /><AccountButton /></div>
  </>;
}
