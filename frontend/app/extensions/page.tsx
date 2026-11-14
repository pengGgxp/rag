"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ChevronRight, FileText, Plug, Plus, Search } from "lucide-react";
import * as Tabs from "../../components/ui/tabs";
import { WorkspaceHeader, WorkspaceSkeleton } from "../workspace-header";
import { useExtensionResource, type MCPConnection } from "./extension-data";

type SkillSummary = { id: string; name: string | null; description: string; enabled?: boolean; installed?: boolean; directory?: string; manifest_error?: string };

export default function ExtensionsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "mcp" ? "mcp" : "skills";
  const query = params.get("q") || "";
  const status = params.get("status") || "";
  const source = params.get("source") === "platform" ? "platform" : "personal";
  function filter(values: Record<string, string>) {
    const next = new URLSearchParams(params.toString());
    for (const [key, value] of Object.entries(values)) { if (value) next.set(key, value); else next.delete(key); }
    router.replace(`/extensions${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  const returnTo = `/extensions${params.size ? `?${params}` : ""}`;
  const skills = useExtensionResource<SkillSummary[]>(tab === "skills" ? source === "personal" ? "/agent/skills" : "/agent/skills/platform" : null);
  const mcp = useExtensionResource<MCPConnection[]>(tab === "mcp" ? "/agent/mcp" : null);
  const resource = tab === "mcp" ? mcp : skills;
  const items = resource.data?.filter(item => `${item.name} ${"description" in item ? item.description : item.url}`.toLowerCase().includes(query.toLowerCase())
    && (!status || (item.enabled !== false ? "enabled" : "disabled") === status));
  return <>
    <WorkspaceHeader title="扩展" actions={<Link className="primary" href={tab === "mcp" ? "/extensions/mcp/new" : "/extensions/skills/new"}>
      <Plus size={16} />{tab === "mcp" ? "添加 MCP" : "添加 Skill"}
    </Link>} />
    <div className="workspace-body extension-page">
      <Tabs.Root value={tab} onValueChange={value => filter({ tab: value, q: "", status: "" })}>
        <Tabs.List className="extension-tabs" aria-label="扩展类型">
          <Tabs.Trigger value="skills"><FileText size={16} />Skills</Tabs.Trigger>
          <Tabs.Trigger value="mcp"><Plug size={16} />MCP</Tabs.Trigger>
        </Tabs.List>
        <div className="extension-toolbar">
          {tab === "skills" && <label>来源<select aria-label="Skill 来源" value={source} onChange={e => filter({ source: e.target.value, status: "" })}><option value="personal">我的 Skill</option><option value="platform">平台 Skill</option></select></label>}
          <label className="extension-search"><Search size={16} /><input aria-label="搜索扩展" placeholder="搜索名称或用途" value={query} onChange={e => filter({ q: e.target.value })} /></label>
          {(tab === "mcp" || source === "personal") && <label>状态<select aria-label="启停筛选" value={status} onChange={e => filter({ status: e.target.value })}><option value="">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>}
        </div>
        {resource.loading ? <WorkspaceSkeleton /> : resource.error ? <div role="alert" className="extension-error">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div>
          : <div className="extension-list">{items?.map(item => <Link className="extension-row" key={item.id}
            href={tab === "mcp" ? `/extensions/mcp/${item.id}` : `/extensions/skills/${item.id}?${source === "platform" ? "platform=1&" : ""}returnTo=${encodeURIComponent(returnTo)}`}>
            <span className="extension-symbol">{tab === "mcp" ? <Plug size={20} /> : <FileText size={20} />}</span>
            <span className="extension-copy"><strong>{item.name || "待修复的 Skill"}</strong><span>{"description" in item ? item.description : item.url}</span>{"directory" in item && <small>{item.directory}</small>}{"manifest_error" in item && item.manifest_error && <small>{item.manifest_error}</small>}</span>
            <span className="extension-meta">{tab === "skills" && source === "platform" ? (item as SkillSummary).installed ? "工作区已有" : "平台提供" : item.enabled ? "已启用" : "已停用"}
              {"allowed_tools" in item && <small>{item.allowed_tools.length} 个工具已授权</small>}</span><ChevronRight size={16} />
          </Link>)}
          {!items?.length && <p className="extension-empty">{query || status ? "没有符合条件的扩展" : tab === "mcp" ? "还没有 MCP 连接" : source === "platform" ? "暂无平台 Skill" : "还没有个人 Skill"}</p>}
        </div>}
      </Tabs.Root>
    </div>
  </>;
}
