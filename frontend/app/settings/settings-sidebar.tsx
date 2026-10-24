"use client";

import Link from "next/link";
import {
  CreditCard,
  Layers3,
  MessageSquare,
  Puzzle,
  ScrollText,
  SlidersHorizontal,
  UserRound,
  UsersRound,
} from "lucide-react";
import type { Role, Session } from "../auth";

export type SettingsSection =
  | "account"
  | "billing"
  | "policies"
  | "members"
  | "audit";

const settingsPath = (section: SettingsSection) =>
  section === "account" ? "/settings" : `/settings/${section}`;

export function SettingsSidebar({
  session,
  role,
  activeSection,
}: {
  session: Session;
  role?: Role;
  activeSection: SettingsSection;
}) {
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
          <span className="settings-nav-group-label">工作台</span>
          <Link href="/">
            <Layers3 size={17} />
            知识库
          </Link>
          <Link href="/?chat=1">
            <MessageSquare size={17} />
            知识问答
          </Link>
          <Link href="/?extensions=1">
            <Puzzle size={17} />
            Agent 扩展
          </Link>
        </div>
        <div className="settings-nav-group settings-nav-group-management">
          <span className="settings-nav-group-label">管理</span>
          <Link
            href={settingsPath("account")}
            data-active={activeSection === "account"}
          >
            <UserRound size={17} />
            我的账号
          </Link>
          <Link
            href={settingsPath("billing")}
            data-active={activeSection === "billing"}
          >
            <CreditCard size={17} />
            积分与充值
          </Link>
          {role === "admin" && (
            <>
              <Link
                href={settingsPath("policies")}
                data-active={activeSection === "policies"}
              >
                <SlidersHorizontal size={17} />
                计费规则
              </Link>
              <Link
                href={settingsPath("members")}
                data-active={activeSection === "members"}
              >
                <UsersRound size={17} />
                工作区成员
              </Link>
              <Link
                href={settingsPath("audit")}
                data-active={activeSection === "audit"}
              >
                <ScrollText size={17} />
                审计日志
              </Link>
            </>
          )}
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
