"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";

export default function PricingLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const workspace = useSearchParams().get("workspace");
  return <>
    <nav className="admin-section-nav" aria-label="计费管理">
      <Link href={`/admin/pricing${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`} aria-current={pathname === "/admin/pricing" ? "page" : undefined}>工作区倍率</Link>
      <Link href="/admin/pricing/template" aria-current={pathname === "/admin/pricing/template" ? "page" : undefined}>平台价格</Link>
    </nav>
    {children}
  </>;
}
