"use client";

import { ResourceLibrary } from "../resource-library";
import { WorkspaceHeader } from "../workspace-header";
import { WorkspaceShell } from "../workspace-shell";

export default function ResourcesPage() {
  return <WorkspaceShell screen="files"><WorkspaceHeader title="工作区" />
    <div className="workspace-body workspace-files-page"><ResourceLibrary /></div>
  </WorkspaceShell>;
}
