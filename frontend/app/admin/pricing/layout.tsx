"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

export default function PricingLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return <>
    <nav className="admin-section-nav" aria-label="计费管理">
      <Link href="/admin/pricing/template" aria-current={pathname === "/admin/pricing/template" ? "page" : undefined}>平台价格</Link>
    </nav>
    {children}
  </>;
}
