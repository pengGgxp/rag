"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { ChevronRight, FileText, Plug, Plus, Search } from "lucide-react";
import * as Tabs from "../../components/ui/tabs";
import { WorkspaceHeader, WorkspaceSkeleton } from "../workspace-header";
import { useExtensionResource, type MCPConnection } from "./extension-data";
import { SkillImport } from "./skill-import";

export default function ExtensionsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "mcp" ? "mcp" : "skills";
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const mcp = useExtensionResource<MCPConnection[]>(tab === "mcp" ? "/agent/mcp" : null);
  const items = mcp.data?.filter(item => `${item.name} ${item.url}`.toLowerCase().includes(query.toLowerCase())
    && (!status || (item.enabled ? "enabled" : "disabled") === status));
  return <>
    <WorkspaceHeader title="扩展" actions={tab === "mcp" ? <Link className="primary" href="/extensions/mcp/new"><Plus size={16} />添加 MCP</Link> : undefined} />
    <div className="workspace-body extension-page">
      <Tabs.Root value={tab} onValueChange={value => { router.push(`/extensions?tab=${value}`); setQuery(""); setStatus(""); }}>
        <Tabs.List className="extension-tabs" aria-label="扩展类型">
          <Tabs.Trigger value="skills"><FileText size={16} />Skills</Tabs.Trigger>
          <Tabs.Trigger value="mcp"><Plug size={16} />MCP</Tabs.Trigger>
        </Tabs.List>
        {tab === "skills" ? <SkillImport /> : <>
          <div className="extension-toolbar">
            <label className="extension-search"><Search size={16} /><input aria-label="搜索扩展" placeholder="搜索名称或用途" value={query} onChange={e => setQuery(e.target.value)} /></label>
            <label>状态<select aria-label="启停筛选" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>
          </div>
          {mcp.loading ? <WorkspaceSkeleton /> : mcp.error ? <div role="alert" className="extension-error">{mcp.error}<button className="secondary" onClick={() => void mcp.reload()}>重试</button></div>
            : <div className="extension-list">{items?.map(item => <Link className="extension-row" key={item.id} href={`/extensions/mcp/${item.id}`}>
              <span className="extension-symbol"><Plug size={20} /></span>
              <span className="extension-copy"><strong>{item.name}</strong><span>{item.url}</span></span>
              <span className="extension-meta">{item.enabled ? "已启用" : "已停用"}<small>{item.allowed_tools.length} 个工具已授权</small></span><ChevronRight size={16} />
            </Link>)}
            {!items?.length && <p className="extension-empty">{query || status ? "没有符合条件的扩展" : "还没有 MCP 连接"}</p>}
          </div>}
        </>}
      </Tabs.Root>
    </div>
  </>;
}
