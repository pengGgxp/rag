"use client";

import { ResourceLibrary } from "../resource-library";
import { WorkspaceHeader } from "../workspace-header";
import { WorkspaceShell } from "../workspace-shell";
import { AdminChangesBoundary } from "../admin/admin-changes";
import "../admin/platform-content.css";

export default function ResourcesPage() {
  return <AdminChangesBoundary guardHistory><WorkspaceShell screen="files"><WorkspaceHeader title="工作区" />
    <div className="workspace-body workspace-files-page"><ResourceLibrary /></div>
  </WorkspaceShell></AdminChangesBoundary>;
}
