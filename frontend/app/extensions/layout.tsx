"use client";
import type { ReactNode } from "react";
import { WorkspaceShell } from "../workspace-shell";
import { useAuth } from "../auth";
import { AdminChangesBoundary } from "../admin/admin-changes";
import "../admin/platform-content.css";
import "./extensions.css";

export default function ExtensionsLayout({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  return <AdminChangesBoundary guardHistory key={session.user.id}>
    <WorkspaceShell screen="extensions">{children}</WorkspaceShell>
  </AdminChangesBoundary>;
}
