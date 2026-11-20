"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ChevronRight, FileText, Plug, Plus, Search } from "lucide-react";
import * as Tabs from "../../components/ui/tabs";
import { WorkspaceHeader, WorkspaceSkeleton } from "../workspace-header";
import { useExtensionResource, type MCPConnection } from "./extension-data";
import { SkillCatalogView } from "./skill-catalog-view";

export default function ExtensionsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "mcp" ? "mcp" : "skills";
  const query = params.get("q") || "";
  const requestedStatus = params.get("status") || "";
  const status = tab === "skills" && !["installed", "available"].includes(requestedStatus) ? "" : requestedStatus;
  function filter(values: Record<string, string>) {
    const next = new URLSearchParams(params.toString());
    next.delete("source");
    for (const [key, value] of Object.entries(values)) { if (value) next.set(key, value); else next.delete(key); }
    router.replace(`/extensions${next.size ? `?${next}` : ""}`, { scroll: false });
  }
  const returnTo = `/extensions${params.size ? `?${params}` : ""}`;
  const mcp = useExtensionResource<MCPConnection[]>(tab === "mcp" ? "/agent/mcp" : null);
  const resource = mcp;
  const items = resource.data?.filter(item => `${item.name} ${item.url}`.toLowerCase().includes(query.toLowerCase())
    && (!status || (item.enabled !== false ? "enabled" : "disabled") === status));
  return <>
    <WorkspaceHeader title={tab === "skills" ? "Skill 仓库" : "扩展"} actions={<Link className="primary skill-add" href={tab === "mcp" ? "/extensions/mcp/new" : `/extensions/skills/new?returnTo=${encodeURIComponent(returnTo)}`}>
      <Plus size={16} />{tab === "mcp" ? "添加 MCP" : "添加 Skill"}
    </Link>} />
    <div className={`workspace-body extension-page ${tab === "skills" ? "skill-catalog-page" : ""}`}><div className={tab === "skills" ? "skill-catalog-content" : undefined}>
      <Tabs.Root value={tab} onValueChange={value => filter({ tab: value, q: "", status: "" })}>
        <Tabs.List className="extension-tabs" aria-label="扩展类型">
          <Tabs.Trigger value="skills"><FileText size={16} />Skills</Tabs.Trigger>
          <Tabs.Trigger value="mcp"><Plug size={16} />MCP</Tabs.Trigger>
        </Tabs.List>
        {tab === "skills" ? <SkillCatalogView query={query} status={status} returnTo={returnTo} filter={filter} /> : <><div className="extension-toolbar">
          <label className="extension-search"><Search size={16} /><input aria-label="搜索扩展" placeholder="搜索名称或用途" value={query} onChange={e => filter({ q: e.target.value })} /></label>
          <label>状态<select aria-label="启停筛选" value={status} onChange={e => filter({ status: e.target.value })}><option value="">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>
        </div>
        {resource.loading ? <WorkspaceSkeleton /> : resource.error ? <div role="alert" className="extension-error">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div>
          : <div className="extension-list">{items?.map(item => <Link className="extension-row" key={item.id}
            href={`/extensions/mcp/${item.id}`}>
            <span className="extension-symbol"><Plug size={20} /></span>
            <span className="extension-copy"><strong>{item.name}</strong><span>{item.url}</span></span>
            <span className="extension-meta">{item.enabled ? "已启用" : "已停用"}
              {"allowed_tools" in item && <small>{item.allowed_tools.length} 个工具已授权</small>}</span><ChevronRight size={16} />
          </Link>)}
          {!items?.length && <p className="extension-empty">{query || status ? "没有符合条件的扩展" : "还没有 MCP 连接"}</p>}
        </div>}</>}
      </Tabs.Root>
    </div></div>
  </>;
}
