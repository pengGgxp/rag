"use client";
import { useAdminResource } from "../admin/admin-ui";
export { json } from "../admin/admin-ui";
export const useExtensionResource = useAdminResource;
export type { PackageReview, SkillFile } from "../admin/skills/page";
import type { Skill } from "../admin/skills/page";
export type PersonalSkill = Skill & { limitations_acknowledged: boolean };
export type MCPConnection = {
  id: string; name: string; url: string; transport: "streamable_http" | "sse";
  enabled: boolean; has_credentials: boolean; header_names: string[];
  version: number; authorization_version: number; last_checked_at: string | null;
  tools: { name: string; description?: string; inputSchema: object }[]; allowed_tools: string[];
};
export type ExtensionSelection = { skill_ids: string[]; mcp_server_ids: string[] };
