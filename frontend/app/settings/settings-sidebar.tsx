"use client";

import Link from "next/link";
import { CreditCard, ScrollText, ShieldCheck, UserRound, HardDrive } from "lucide-react";
import { visibleSections, type SettingsSection } from "./settings-routes";

const icons = { account: UserRound, security: ShieldCheck,
  billing: CreditCard, audit: ScrollText, storage: HardDrive };

export function SettingsSidebar({ activeSection, hrefFor, navigate }: {
  activeSection?: SettingsSection;
  hrefFor: (path: string) => string; navigate: (path: string) => void;
}) {
  return <aside className="settings-sidebar" aria-label="设置导航">
    <nav className="settings-nav-group" aria-label="个人">
      <p className="settings-nav-label">个人</p>
      {visibleSections().map(section => {
        const Icon = icons[section.key];
        return <Link key={section.key} href={hrefFor(section.path)} replace scroll={false}
          className="navigation-item" aria-current={activeSection === section.key ? "page" : undefined}
          onClick={event => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
            event.preventDefault(); navigate(section.path);
          } }}><Icon size={18} />{section.title}</Link>;
      })}
    </nav>
  </aside>;
}
