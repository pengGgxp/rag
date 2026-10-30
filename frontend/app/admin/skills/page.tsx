"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Plus, Search } from "lucide-react";
import { AdminHeading, AdminLoad, date, Pagination, type Page, useAdminResource } from "../admin-ui";
import "../platform-content.css";

export type Skill = { id: string; name: string; description: string; markdown: string; enabled: boolean; version: number; updated_at: string };

export default function SkillsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const resource = useAdminResource<Page<Skill>>(`/site/admin/skills?${params}`);
  return <>
    <AdminHeading title="Skill 管理"><Link className="primary" href="/admin/skills/new"><Plus size={16} />新建 Skill</Link></AdminHeading>
    <form key={params.toString()} className="admin-filters" onSubmit={event => {
      event.preventDefault(); const data = new FormData(event.currentTarget); const next = new URLSearchParams();
      for (const [key, value] of data) if (value !== "" && value !== "all") next.set(key, String(value));
      router.push(`/admin/skills?${next}`);
    }}>
      <label>搜索<input name="q" aria-label="搜索 Skill" placeholder="名称" maxLength={200} defaultValue={params.get("q") ?? ""} /></label>
      <label>状态<select name="enabled" defaultValue={params.get("enabled") ?? "all"}><option value="all">全部状态</option><option value="true">已启用</option><option value="false">已停用</option></select></label>
      <button className="secondary" type="submit"><Search size={16} />查询</button>
    </form>
    <AdminLoad {...resource} retry={resource.reload}>
      <div className="managed-list" aria-label="Skill 列表">
        {resource.data?.items.map(skill => <Link className="managed-row" href={`/admin/skills/${skill.id}`} key={skill.id}>
          <div className="managed-main"><strong>{skill.name}</strong><p>{skill.description}</p></div>
          <span className="admin-status">{skill.enabled ? "已启用" : "已停用"}</span>
          <time>{date(skill.updated_at)}</time>
        </Link>)}
      </div>
      {resource.data && !resource.data.items.length && <p className="admin-empty">暂无符合条件的 Skill</p>}
      {resource.data && <Pagination total={resource.data.total} page={resource.data.page} pageSize={resource.data.page_size} change={page => {
        const next = new URLSearchParams(params); next.set("page", String(page)); router.push(`/admin/skills?${next}`);
      }} />}
    </AdminLoad>
  </>;
}
