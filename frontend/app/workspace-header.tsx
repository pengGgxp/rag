"use client";

import type { ReactNode } from "react";
import { Menu } from "lucide-react";
import { Tooltip } from "../components/ui/tooltip";
import { useUiStore } from "./ui-store";
import { SiteAnnouncementBanner } from "./site-announcement";

export function WorkspaceHeader({ title, context, actions, titleContent }: {
  title: string;
  titleContent?: ReactNode;
  context?: ReactNode;
  actions?: ReactNode;
}) {
  const openNavigation = useUiStore((state) => state.setSidebarOpen);
  return <><header className="workspace-header">
    <Tooltip content="打开导航">
      <button className="icon-button mobile-only" data-workspace-menu
        aria-label="打开导航" aria-haspopup="dialog"
        onClick={() => openNavigation(true)}><Menu size={19} /></button>
    </Tooltip>
    <div className="workspace-header-title">
      {titleContent ?? <Tooltip content={title}><h1 tabIndex={0}>{title}</h1></Tooltip>}
      {context && <span className="workspace-header-context">{context}</span>}
    </div>
    {actions && <div className="workspace-header-actions">{actions}</div>}
  </header><SiteAnnouncementBanner /></>;
}

export function WorkspaceSkeleton() {
  return <div className="workspace-skeleton" role="status" aria-label="正在加载">
    <span className="sr-only">正在加载</span>
    {Array.from({ length: 3 }, (_, index) => <div className="skeleton" aria-hidden="true" key={index} />)}
  </div>;
}
