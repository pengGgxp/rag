"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Dialog } from "../../components/ui/dialog";
import { useSettingsNavigation } from "./settings-navigation";
import { SettingsSidebar } from "./settings-sidebar";
import { safeReturnTo, sectionFromPath, visibleSections } from "./settings-routes";
import "./settings.css";

const SettingsForm = createContext({ setDirty: (_dirty: boolean) => {}, setBusy: (_busy: boolean) => {} });
export const useSettingsForm = () => useContext(SettingsForm);

export default function SettingsFrame({ intercepted = false, openingHref, onClose, children }: {
  intercepted?: boolean; openingHref?: string | null; onClose?: () => void; children: ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { fromWorkspace, navigate: navigateIntercepted } = useSettingsNavigation();
  const origin = useRef(intercepted && fromWorkspace);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<(() => void) | null>(null);
  const confirmation = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const pendingUrl = openingHref ? new URL(openingHref, "https://internal.invalid") : null;
  const displayPath = pendingUrl?.pathname ?? pathname;
  const section = sectionFromPath(displayPath);
  const returnTo = safeReturnTo(pendingUrl?.searchParams.get("returnTo") ?? searchParams.get("returnTo"));
  const hrefFor = (path: string) => `${path}?returnTo=${encodeURIComponent(returnTo)}`;
  const navigate = (path: string) => {
    const href = hrefFor(path);
    if (intercepted) navigateIntercepted(href);
    else router.replace(href, { scroll: false });
  };
  const run = (action: () => void) => {
    if (busy) return;
    if (dirty) { returnFocus.current = document.activeElement as HTMLElement; setPending(() => action); }
    else action();
  };
  const close = () => run(() => onClose ? onClose() : origin.current ? router.back() : router.replace(returnTo));
  useEffect(() => { if (pending) confirmation.current?.focus(); }, [pending]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  return <Dialog title={section?.title ?? "设置"} settingsWindow busy={busy} onClose={close}
    restoreFocusSelector={origin.current ? '[data-account-trigger="true"]' : undefined}>
    <SettingsForm.Provider value={{ setDirty, setBusy }}>
      <div className="settings-window-layout">
        <SettingsSidebar activeSection={section?.key}
          hrefFor={hrefFor} navigate={path => run(() => navigate(path))} />
        <main className="settings-main">
          <header className="settings-header">
            <h1>{section?.title ?? (displayPath === "/settings/policies" ? "计费规则" : "页面不存在")}</h1>
            <select className="settings-mobile-select" aria-label="选择设置页面" value={section?.key ?? ""}
              disabled={busy} onChange={event => {
                const next = visibleSections().find(item => item.key === event.target.value);
                if (next) run(() => navigate(next.path));
              }}>
              {!section && <option value="">选择页面</option>}
              <optgroup label="个人">
                {visibleSections().map(item =>
                  <option key={item.key} value={item.key}>{item.title}</option>)}
              </optgroup>
            </select>
          </header>
          {pending && <div className="settings-discard" role="alert" tabIndex={-1} ref={confirmation}>
            <span>有尚未保存的修改，要放弃吗？</span>
            <button className="secondary" onClick={() => { setPending(null); returnFocus.current?.focus(); }}>继续编辑</button>
            <button className="danger" onClick={() => { const action = pending; setPending(null); setDirty(false); action(); }}>放弃修改</button>
          </div>}
          <div className="settings-body">{children}</div>
        </main>
      </div>
    </SettingsForm.Provider>
  </Dialog>;
}
