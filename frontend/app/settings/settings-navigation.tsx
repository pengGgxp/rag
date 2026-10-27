"use client";

import { createContext, useContext, useRef, type ReactNode } from "react";
import { usePathname } from "next/navigation";

const SettingsOrigin = createContext(false);

export function SettingsNavigation({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const fromWorkspace = useRef(false);
  // Internal section navigation may remount an intercepted route; retain how this settings visit began.
  if (!pathname.startsWith("/settings")) fromWorkspace.current = pathname === "/";
  return <SettingsOrigin.Provider value={fromWorkspace.current}>{children}</SettingsOrigin.Provider>;
}

export function useSettingsFromWorkspace() {
  return useContext(SettingsOrigin);
}
