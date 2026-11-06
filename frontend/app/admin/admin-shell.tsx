"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Building2,
  Coins,
  CreditCard,
  Cpu,
  HardDrive,
  Globe,
  LayoutDashboard,
  ScrollText,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  UsersRound,
  FileText,
  MessageSquare,
  Monitor,
} from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useAuth } from "../auth";
import { AdminChangesBoundary } from "./admin-changes";

const navigation = [
  { href: "/admin", label: "概览", icon: LayoutDashboard },
  { href: "/admin/users", label: "用户", icon: UsersRound },
  { href: "/admin/workspaces", label: "工作区", icon: Building2 },
  { href: "/admin/models", label: "模型管理", icon: Cpu },
  { href: "/admin/search", label: "联网搜索", icon: Globe },
  { href: "/admin/skills", label: "Skill 管理", icon: FileText },
  { href: "/admin/feedback", label: "意见反馈", icon: MessageSquare },
  { href: "/admin/pricing", label: "计费管理", icon: Coins },
  { href: "/admin/payments", label: "支付设置", icon: CreditCard },
  { href: "/admin/storage", label: "文件存储", icon: HardDrive },
  { href: "/admin/cloud-computer", label: "云电脑", icon: Monitor },
  { href: "/admin/audit", label: "站点审计", icon: ScrollText },
  { href: "/admin/auth", label: "认证与登录", icon: Settings2 },
  { href: "/admin/settings", label: "站点设置", icon: SlidersHorizontal },
];

export function AdminShell({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  const pathname = usePathname();
  const workspaceId = useSearchParams().get("workspace");
  const navigationRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navigationRef.current;
    if (!nav) return;
    const revealCurrent = () => {
      const current = nav.querySelector<HTMLElement>('[aria-current="page"]');
      if (!current || nav.scrollWidth <= nav.clientWidth) return;
      const linkBounds = current.getBoundingClientRect();
      const navBounds = nav.getBoundingClientRect();
      if (linkBounds.left < navBounds.left || linkBounds.right > navBounds.right) {
        nav.scrollLeft += linkBounds.left - navBounds.left - 16;
      }
    };
    revealCurrent();
    const observer = new ResizeObserver(revealCurrent);
    observer.observe(nav);
    return () => observer.disconnect();
  }, [pathname]);
  if (!session.user.is_site_admin)
    return (
      <main className="site-auth-denied">
        <ShieldCheck size={28} />
        <h1>无权访问站点管理</h1>
        <p>此区域仅对站点管理员开放。</p>
        <Link className="secondary" href="/">
          回到工作台
        </Link>
      </main>
    );
  return (
    <AdminChangesBoundary><div className="admin-shell">
      <aside className="admin-sidebar" aria-label="站点管理">
        <Link className="admin-brand" href="/admin">
          <span className="brand-mark">
            <img src="/icon.svg" alt="" />
          </span>
          <strong>知库</strong>
          <span>站点管理</span>
        </Link>
        <nav ref={navigationRef} aria-label="后台导航">
          {navigation.map(({ href, label, icon: Icon }) => {
            const active =
              href === "/admin" ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                key={href}
                className="navigation-item"
                href={workspaceId && href === "/admin/pricing" ? `${href}?workspace=${encodeURIComponent(workspaceId)}` : href}
                aria-current={active ? "page" : undefined}
              >
                <Icon size={18} />
                {label}
              </Link>
            );
          })}
        </nav>
        <footer>
          <span>{session.user.name}</span>
          <Link href="/">
            <ArrowLeft size={16} />
            用户工作台
          </Link>
        </footer>
      </aside>
      <main className="admin-main">{children}</main>
    </div></AdminChangesBoundary>
  );
}
