"use client";
import { AdminHeading } from "../admin-ui";
import { SkillImport } from "../../extensions/skill-import";
import "../platform-content.css";
export function SkillEditor({ id }: { id?: string }) {
  return <><AdminHeading title="导入 Skill" /><SkillImport id={id} /></>;
}
