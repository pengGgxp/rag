import type { ReactNode } from "react";
import SettingsFrame from "./standalone-settings";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return <SettingsFrame>{children}</SettingsFrame>;
}
