"use client";

import { useCallback, useEffect, useId, useRef, useState, type MouseEvent } from "react";
import { request } from "./api";
import { ResourcePreview } from "./resource-preview";
import type { ResourceFile } from "./resource-library";

export function plainLinkClick(event: MouseEvent<HTMLAnchorElement>) {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

// One opening path for inline links, message files and resource-page deep links.
export function useResourceFilePreview(scope: string) {
  const [item, setItem] = useState<ResourceFile | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null), [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const triggerId = useId();
  const focusSelector = useRef(`[data-file-preview-trigger="${triggerId}"]`);
  useEffect(() => {
    setItem(null); setBusyId(null); setError("");
    return () => controller.current?.abort();
  }, [scope]);
  const close = useCallback(() => { controller.current?.abort(); setItem(null); }, []);
  const open = useCallback(async (id: string, trigger?: HTMLElement) => {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    if (trigger) {
      const marker = trigger.getAttribute("data-file-preview-trigger") || triggerId;
      trigger.setAttribute("data-file-preview-trigger", marker);
      focusSelector.current = `[data-file-preview-trigger="${marker}"]`;
    }
    setBusyId(id); setError(""); setItem(null);
    try {
      const value = await request<ResourceFile>(`/resources/${encodeURIComponent(id)}`, { signal: current.signal });
      if (!current.signal.aborted) setItem(value);
    } catch (cause) {
      if (!current.signal.aborted) setError(cause instanceof Error ? cause.message : "文件无法打开，请稍后重试");
    } finally { if (!current.signal.aborted) setBusyId(null); }
  }, [triggerId]);
  return { open, busyId, error, preview: item && <ResourcePreview key={`${scope}:${item.id}`} item={item}
    scope={scope} onClose={close} restoreFocusSelector={focusSelector.current} /> };
}
