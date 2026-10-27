import type { ReactNode } from "react";
import SettingsFrame from "../../../settings/standalone-settings";

export default function InterceptedSettingsLayout({ children }: { children: ReactNode }) {
  return <SettingsFrame intercepted>{children}</SettingsFrame>;
}
