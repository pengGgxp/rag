"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import SettingsFrame from "./standalone-settings";
import { useSettingsNavigation } from "./settings-navigation";
import SettingsLoading from "./settings-loading";
import SettingsError from "./settings-error";

function PendingSettings({ href }: { href: string }) {
  const { navigate } = useSettingsNavigation();
  const [timedOut, setTimedOut] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setTimedOut(false);
    const timer = window.setTimeout(() => setTimedOut(true), 15_000);
    return () => window.clearTimeout(timer);
  }, [href, attempt]);
  return timedOut ? <SettingsError reset={() => {
    setAttempt(value => value + 1);
    navigate(href);
  }} /> : <SettingsLoading />;
}

// The workspace owns the sole dialog; intercepted routes only supply its body.
export function SettingsWorkspaceHost({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { openingHref, closing, close } = useSettingsNavigation();
  const committed = pathname.startsWith("/settings");
  if (closing || (!openingHref && !committed)) return null;
  return <SettingsFrame intercepted openingHref={openingHref} onClose={close}>
    {committed ? children : openingHref && <PendingSettings href={openingHref} />}
  </SettingsFrame>;
}
