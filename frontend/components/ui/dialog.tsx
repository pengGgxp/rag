"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { motion, useReducedMotion } from "motion/react";
import { X } from "lucide-react";
import { CSSProperties, ReactNode, useLayoutEffect, useRef, useState } from "react";
import { Tooltip } from "./tooltip";

export function Dialog({
  title,
  children,
  onClose,
  busy = false,
  wide = false,
  fullScreen = false,
  sidePanel = false,
  settingsWindow = false,
  restoreFocusSelector,
  anchor,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  wide?: boolean;
  fullScreen?: boolean;
  sidePanel?: boolean;
  settingsWindow?: boolean;
  restoreFocusSelector?: string;
  anchor?: HTMLElement | null;
}) {
  const contentRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();
  const interactedOutside = useRef(false);
  const [position, setPosition] = useState<CSSProperties>();
  useLayoutEffect(() => {
    if (!anchor) return;
    interactedOutside.current = false;
    const update = () => {
      const rect = anchor.getBoundingClientRect();
      const width = Math.min(360, window.innerWidth - 24);
      const above = rect.top >= 240;
      setPosition({
        width,
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        ...(above ? { bottom: window.innerHeight - rect.top + 8, top: "auto" }
          : { top: rect.bottom + 8, bottom: "auto" }),
        maxHeight: Math.max(120, above ? rect.top - 20 : window.innerHeight - rect.bottom - 20),
      });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchor]);
  const closeButton = (
    <DialogPrimitive.Close asChild>
      <button className="icon-button" aria-label="关闭" title="关闭" disabled={busy}>
        <X size={18} />
      </button>
    </DialogPrimitive.Close>
  );

  return (
    <DialogPrimitive.Root
      open
      modal={!anchor}
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogPrimitive.Portal>
        {!anchor && <DialogPrimitive.Overlay className="modal-overlay" />}
        <DialogPrimitive.Content
          ref={contentRef}
          className={anchor ? "selection-surface" : `modal ${wide ? "modal-wide" : ""} ${fullScreen ? "modal-fullscreen" : ""} ${sidePanel ? "modal-side-panel" : ""} ${settingsWindow ? "modal-settings" : ""}`}
          style={anchor ? position : undefined}
          onOpenAutoFocus={event => {
            if (!anchor) return;
            event.preventDefault();
            const content = contentRef.current;
            const target = content?.querySelector<HTMLElement>("input")
              ?? content?.querySelector<HTMLElement>('[aria-selected="true"]:not(:disabled)')
              ?? content?.querySelector<HTMLElement>('[role="option"]:not(:disabled)');
            target?.focus();
          }}
          onCloseAutoFocus={(event) => {
            if (anchor) {
              event.preventDefault();
              const anotherDialog = Array.from(document.querySelectorAll('[role="dialog"][data-state="open"]'))
                .some(element => element !== contentRef.current);
              if (!interactedOutside.current && !anotherDialog && anchor.isConnected) anchor.focus();
              return;
            }
            if (!restoreFocusSelector) return;
            event.preventDefault();
            requestAnimationFrame(() => {
              if (document.querySelector('[role="dialog"][data-state="open"]')) return;
              const target = Array.from(document.querySelectorAll<HTMLElement>(restoreFocusSelector))
                .find(element => element.getClientRects().length > 0);
              (target ?? document.querySelector<HTMLElement>("[data-workspace-menu]"))?.focus();
            });
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
          onInteractOutside={event => {
            if (!anchor) return;
            if (anchor.contains(event.target as Node)) event.preventDefault();
            else interactedOutside.current = true;
          }}
        >
          <motion.div
            initial={fullScreen ? { opacity: 0 } : anchor ? { opacity: 0, y: 4 } : { opacity: 0, scale: 0.97, y: 6 }}
            animate={fullScreen ? { opacity: 1 } : { opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: anchor && reducedMotion ? 0 : 0.16, ease: "easeOut" }}
          >
            <div className={anchor ? "sr-only" : settingsWindow ? "settings-dialog-heading" : "modal-heading"}>
              <DialogPrimitive.Title>{title}</DialogPrimitive.Title>
              {!anchor && (fullScreen || sidePanel || settingsWindow ? closeButton : <Tooltip content="关闭">{closeButton}</Tooltip>)}
            </div>
            <DialogPrimitive.Description className="sr-only">
              {title}
            </DialogPrimitive.Description>
            {children}
          </motion.div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
