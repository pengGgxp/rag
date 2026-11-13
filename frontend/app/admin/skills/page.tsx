"use client";
import Link from "next/link";
import { Plus } from "lucide-react";
import { AdminHeading } from "../admin-ui";
import "../platform-content.css";


export default function SkillsPage() {
  return <><AdminHeading title="Skill 资源"><Link className="primary" href="/admin/skills/new"><Plus size={16} />导入 Skill</Link></AdminHeading>
    <p><Link href="/resources">在资源库查看 Skill 文件</Link></p></>;
}
