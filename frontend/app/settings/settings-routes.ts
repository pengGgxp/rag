export const settingsSections = [
  { key: "account", title: "个人资料", path: "/settings", group: "personal" },
  { key: "security", title: "登录与安全", path: "/settings/security", group: "personal" },
  { key: "billing", title: "积分与充值", path: "/settings/billing", group: "personal" },
  { key: "storage", title: "存储与下载", path: "/settings/storage", group: "personal" },
  { key: "audit", title: "操作记录", path: "/settings/audit", group: "personal" },
] as const;
export type SettingsSection = typeof settingsSections[number]["key"];
export function sectionFromPath(path: string) {
  return settingsSections.find(section => section.path === path.replace(/\/$/, ""));
}
export function visibleSections() {
  return settingsSections;
}
export function safeReturnTo(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//") ||
      /[\\\u0000-\u0020]/.test(value)) return "/";
  try {
    const parsed = new URL(value, "https://internal.invalid");
    if (parsed.origin !== "https://internal.invalid" || parsed.pathname.startsWith("/settings")) return "/";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch { return "/"; }
}
