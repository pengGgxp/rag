"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  CreditCard,
  LayoutDashboard,
  Layers3,
  ScrollText,
  SlidersHorizontal,
  UserRound,
  UsersRound,
} from "lucide-react";
import type { Role, Session } from "../auth";

export type SettingsSection =
  | "account"
  | "workspace"
  | "billing"
  | "policies"
  | "members"
  | "audit";

const settingsPath = (section: SettingsSection, returnTo: string) =>
  `${section === "account" ? "/settings" : `/settings/${section}`}?returnTo=${encodeURIComponent(returnTo)}`;

export function SettingsSidebar({
  session,
  role,
  activeSection,
}: {
  session: Session;
  role?: Role;
  activeSection: SettingsSection;
}) {
  const searchParams = useSearchParams();
  const requestedReturnTo = searchParams.get("returnTo");
  const returnTo = requestedReturnTo?.startsWith("/") &&
    !requestedReturnTo.startsWith("//") && !requestedReturnTo.includes("\\") &&
    !requestedReturnTo.startsWith("/settings")
    ? requestedReturnTo
    : "/";
  return (
    <aside className="settings-global-sidebar" aria-label="产品导航">
      <Link className="settings-brand" href="/">
        <span className="brand-mark">
          <Layers3 size={21} />
        </span>
        <span>知库</span>
      </Link>
      <nav className="settings-global-nav">
        <div className="settings-nav-group">
          <span className="settings-nav-group-label">工作区</span>
          <Link
            href={settingsPath("workspace", returnTo)}
            data-active={activeSection === "workspace"}
          >
            <LayoutDashboard size={17} />
            工作区概览
          </Link>
          <Link
            href={settingsPath("billing", returnTo)}
            data-active={activeSection === "billing"}
          >
            <CreditCard size={17} />
            积分与充值
          </Link>
          {role === "admin" && (
            <>
              <Link href={settingsPath("policies", returnTo)} data-active={activeSection === "policies"}>
                <SlidersHorizontal size={17} />计费规则
              </Link>
              <Link href={settingsPath("members", returnTo)} data-active={activeSection === "members"}>
                <UsersRound size={17} />工作区成员
              </Link>
              <Link href={settingsPath("audit", returnTo)} data-active={activeSection === "audit"}>
                <ScrollText size={17} />审计日志
              </Link>
            </>
          )}
        </div>
        <div className="settings-nav-group settings-nav-group-management">
          <span className="settings-nav-group-label">账号</span>
          <Link
            href={settingsPath("account", returnTo)}
            data-active={activeSection === "account"}
          >
            <UserRound size={17} />
            我的账号
          </Link>
        </div>
      </nav>
      <div className="settings-global-footer">
        <div className="settings-user-summary">
          <span className="account-avatar">
            <UserRound size={16} />
          </span>
          <span>
            <strong>{session.user.name}</strong>
            <small>
              {session.workspaces.find(
                (item) => item.id === session.workspace_id,
              )?.name ?? "当前工作区"}
            </small>
          </span>
        </div>
      </div>
    </aside>
  );
}
