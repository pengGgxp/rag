"use client";

import Link from "next/link";
import { Building2, CreditCard, ScrollText, ShieldCheck, UserRound, UsersRound } from "lucide-react";
import type { Role } from "../auth";
import { visibleSections, type SettingsSection } from "./settings-routes";

const icons = { account: UserRound, security: ShieldCheck, workspace: Building2,
  members: UsersRound, billing: CreditCard, audit: ScrollText };

export function SettingsSidebar({ workspaceName, role, activeSection, hrefFor, navigate }: {
  workspaceName: string; role?: Role; activeSection?: SettingsSection;
  hrefFor: (path: string) => string; navigate: (path: string) => void;
}) {
  return <aside className="settings-sidebar" aria-label="设置导航">
    {(["personal", "workspace"] as const).map(group => <nav key={group}
      className="settings-nav-group" aria-label={group === "personal" ? "个人" : "工作区"}>
      <p className="settings-nav-label" title={group === "personal" ? "个人" : workspaceName}>
        {group === "personal" ? "个人" : workspaceName}
      </p>
      {visibleSections(role).filter(section => section.group === group).map(section => {
        const Icon = icons[section.key];
        return <Link key={section.key} href={hrefFor(section.path)} replace scroll={false}
          className="navigation-item" aria-current={activeSection === section.key ? "page" : undefined}
          onClick={event => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
            event.preventDefault(); navigate(section.path);
          } }}><Icon size={18} />{section.title}</Link>;
      })}
    </nav>)}
  </aside>;
}
