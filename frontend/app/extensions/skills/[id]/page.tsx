"use client";
import { use } from "react";
import { WorkspaceHeader } from "../../../workspace-header";
import { SkillImport } from "../../skill-import";
export default function SkillPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <><WorkspaceHeader title="Skill 资源" /><div className="workspace-body extension-page"><SkillImport id={id === "new" ? undefined : id} /></div></>;
}
