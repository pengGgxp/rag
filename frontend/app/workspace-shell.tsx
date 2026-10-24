"use client";

import type { ReactNode } from "react";

export function WorkspaceShell({
  sidebarOpen,
  onClose,
  sidebar,
  children,
}: {
  sidebarOpen: boolean;
  onClose: () => void;
  sidebar: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="workspace">
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={onClose}
        />
      )}
      <aside
        className={`sidebar ${sidebarOpen ? "is-open" : ""}`}
        aria-label="工作空间导航"
      >
        {sidebar}
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}
