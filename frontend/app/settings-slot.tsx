"use client";

import type { ReactNode } from "react";
import { useSelectedLayoutSegment } from "next/navigation";

export function SettingsSlot({ children }: { children: ReactNode }) {
  const segment = useSelectedLayoutSegment();
  // Direct settings pages already own their dialog; only workspaces need the intercepted layer.
  return segment === "settings" ? null : children;
}
