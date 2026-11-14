export type SkillSummary = { id: string; name: string | null; description: string; directory?: string; manifest_error?: string | null };
export type CatalogSkill = { key: string; name: string; description: string; packageId?: string; installations: SkillSummary[]; needsRepair: boolean };

export function skillCatalog(packages: SkillSummary[], installed: SkillSummary[]): CatalogSkill[] {
  const groups = new Map<string, CatalogSkill>();
  for (const item of packages) {
    const key = item.name ? `name:${item.name}` : `package:${item.id}`;
    if (!groups.has(key)) groups.set(key, { key, name: item.name || "未命名 Skill", description: item.description, packageId: item.id, installations: [], needsRepair: false });
  }
  for (const item of installed) {
    const key = item.name ? `name:${item.name}` : `file:${item.id}`;
    let entry = groups.get(key);
    if (!entry) { entry = { key, name: item.name || item.directory?.split("/").pop() || "待修复的 Skill", description: item.description, installations: [], needsRepair: false }; groups.set(key, entry); }
    entry.installations.push(item);
    entry.needsRepair ||= Boolean(item.manifest_error);
    if (entry.installations.length === 1 && item.description) entry.description = item.description;
  }
  return Array.from(groups.values());
}

export function filterSkills(items: CatalogSkill[], query: string, status: string) {
  const text = query.trim().toLocaleLowerCase();
  return items.filter(item => (!status || (item.installations.length ? "installed" : "available") === status)
    && `${item.name} ${item.description} ${item.installations.map(value => value.directory).join(" ")}`.toLocaleLowerCase().includes(text));
}
