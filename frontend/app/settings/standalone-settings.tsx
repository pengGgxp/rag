"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Dialog } from "../../components/ui/dialog";
import SettingsPage from "./settings-page";

function safeReturnTo(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//") ||
      value.includes("\\") || value.startsWith("/settings")) return "/";
  return value;
}

export default function StandaloneSettings({ intercepted = false }: { intercepted?: boolean }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  return (
    <Dialog
      title="设置"
      fullScreen
      restoreFocusSelector={intercepted ? '[data-account-trigger=\"true\"]' : undefined}
      onClose={() => {
        if (intercepted) router.back();
        else router.replace(safeReturnTo(searchParams.get("returnTo")));
      }}
    >
      <SettingsPage standalone={false} />
    </Dialog>
  );
}
