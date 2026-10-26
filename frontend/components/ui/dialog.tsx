"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { motion } from "motion/react";
import { X } from "lucide-react";
import { ReactNode } from "react";
import { Tooltip } from "./tooltip";

export function Dialog({
  title,
  children,
  onClose,
  busy = false,
  wide = false,
  fullScreen = false,
  sidePanel = false,
  restoreFocusSelector,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  wide?: boolean;
  fullScreen?: boolean;
  sidePanel?: boolean;
  restoreFocusSelector?: string;
}) {
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
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="modal-overlay" />
        <DialogPrimitive.Content
          className={`modal ${wide ? "modal-wide" : ""} ${fullScreen ? "modal-fullscreen" : ""} ${sidePanel ? "modal-side-panel" : ""}`}
          onCloseAutoFocus={(event) => {
            if (!restoreFocusSelector) return;
            event.preventDefault();
            requestAnimationFrame(() => {
              document.querySelector<HTMLElement>(restoreFocusSelector)?.focus();
            });
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <motion.div
            initial={fullScreen ? { opacity: 0 } : { opacity: 0, scale: 0.97, y: 6 }}
            animate={fullScreen ? { opacity: 1 } : { opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: 0.16, ease: "easeOut" }}
          >
            <div className="modal-heading">
              <DialogPrimitive.Title>{title}</DialogPrimitive.Title>
              {fullScreen ? closeButton : <Tooltip content="关闭">{closeButton}</Tooltip>}
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
