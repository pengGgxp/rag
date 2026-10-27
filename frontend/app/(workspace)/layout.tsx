import type { ReactNode } from "react";
import { SettingsWorkspaceHost } from "../settings/settings-workspace-host";

export default function WorkspaceLayout({ children, settings }: {
  children: ReactNode;
  settings: ReactNode;
}) {
  return <>{children}<SettingsWorkspaceHost>{settings}</SettingsWorkspaceHost></>;
}
