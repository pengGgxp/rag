"use client";

import { createContext, useContext, useEffect, useRef, useState, useTransition, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useUiStore } from "../ui-store";
import { safeReturnTo } from "./settings-routes";

type Navigation = {
  fromWorkspace: boolean;
  openingHref: string | null;
  closing: boolean;
  open: (href: string) => void;
  navigate: (href: string) => void;
  close: () => void;
};
const SettingsContext = createContext<Navigation | null>(null);

export function SettingsNavigation({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const fromWorkspace = useRef(false);
  const returnTo = useRef("/");
  const [openingHref, setOpeningHref] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const [routing, startTransition] = useTransition();
  // Internal section navigation may remount an intercepted route; retain how this settings visit began.
  if (!pathname.startsWith("/settings")) fromWorkspace.current = pathname === "/";
  useEffect(() => {
    if (openingHref && pathname === new URL(openingHref, window.location.origin).pathname)
      setOpeningHref(null);
    if (!routing && !pathname.startsWith("/settings") && !openingHref)
      setClosing(false);
    // A newer close navigation supersedes any outstanding route response.
    if (closing && !routing && pathname.startsWith("/settings"))
      startTransition(() => router.replace(returnTo.current, { scroll: false }));
  }, [pathname, openingHref, closing, routing, router]);
  return <SettingsContext.Provider value={{
    fromWorkspace: fromWorkspace.current, openingHref, closing,
    open: href => {
      if (openingHref || pathname.startsWith("/settings")) return;
      const target = new URL(href, window.location.origin);
      returnTo.current = safeReturnTo(target.searchParams.get("returnTo"));
      useUiStore.getState().setSidebarOpen(false);
      if (pathname === "/") setOpeningHref(href);
      setClosing(false);
      startTransition(() => router.push(href, { scroll: false }));
    },
    close: () => {
      setClosing(true);
      setOpeningHref(null);
      startTransition(() => {
        if (pathname.startsWith("/settings")) router.back();
        else router.replace(returnTo.current, { scroll: false });
      });
    },
    navigate: href => {
      if (openingHref) setOpeningHref(href);
      startTransition(() => {
        if (openingHref) router.push(href, { scroll: false });
        else router.replace(href, { scroll: false });
      });
    },
  }}>{children}</SettingsContext.Provider>;
}

export function useSettingsNavigation() {
  const navigation = useContext(SettingsContext);
  if (!navigation) throw new Error("Missing settings navigation");
  return navigation;
}
