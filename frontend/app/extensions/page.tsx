"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { ChevronRight, FileText, Plug, Plus, Search, Upload } from "lucide-react";
import * as Tabs from "../../components/ui/tabs";
import { WorkspaceHeader, WorkspaceSkeleton } from "../workspace-header";
import { useExtensionResource, type MCPConnection, type PersonalSkill } from "./extension-data";

export default function ExtensionsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "mcp" ? "mcp" : "skills";
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("personal");
  const skills = useExtensionResource<PersonalSkill[]>(tab === "skills" ? source === "personal" ? "/agent/skills" : "/agent/skills/platform" : null);
  const mcp = useExtensionResource<MCPConnection[]>(tab === "mcp" ? "/agent/mcp" : null);
  const resource = tab === "mcp" ? mcp : skills;
  const items = resource.data?.filter(item => `${item.name} ${"description" in item ? item.description : item.url}`.toLowerCase().includes(query.toLowerCase())
    && (!status || (item.enabled !== false ? "enabled" : "disabled") === status));
  return <>
    <WorkspaceHeader title="扩展" actions={<Link className="primary" href={tab === "mcp" ? "/extensions/mcp/new" : "/extensions/skills/new"}>
      {tab === "mcp" ? <Plus size={16} /> : <Upload size={16} />}{tab === "mcp" ? "添加 MCP" : "添加 Skill"}
    </Link>} />
    <div className="workspace-body extension-page">
      <Tabs.Root value={tab} onValueChange={value => { router.push(`/extensions?tab=${value}`); setQuery(""); setStatus(""); }}>
        <Tabs.List className="extension-tabs" aria-label="扩展类型">
          <Tabs.Trigger value="skills"><FileText size={16} />Skills</Tabs.Trigger>
          <Tabs.Trigger value="mcp"><Plug size={16} />MCP</Tabs.Trigger>
        </Tabs.List>
        <div className="extension-toolbar">
          {tab === "skills" && <label>来源<select aria-label="Skill 来源" value={source} onChange={e => setSource(e.target.value)}><option value="personal">我的 Skill</option><option value="platform">平台 Skill</option></select></label>}
          <label className="extension-search"><Search size={16} /><input aria-label="搜索扩展" placeholder="搜索名称或用途" value={query} onChange={e => setQuery(e.target.value)} /></label>
          {(tab === "mcp" || source === "personal") && <label>状态<select aria-label="启停筛选" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></label>}
        </div>
        {resource.loading ? <WorkspaceSkeleton /> : resource.error ? <div role="alert" className="extension-error">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div>
          : <div className="extension-list">{items?.map(item => <Link className="extension-row" key={item.id}
            href={tab === "mcp" ? `/extensions/mcp/${item.id}` : `/extensions/skills/${item.id}${source === "platform" ? "?platform=1" : ""}`}>
            <span className="extension-symbol">{tab === "mcp" ? <Plug size={20} /> : <FileText size={20} />}</span>
            <span className="extension-copy"><strong>{item.name}</strong><span>{"description" in item ? item.description : item.url}</span></span>
            <span className="extension-meta">{tab === "skills" && source === "platform" ? "平台提供" : item.enabled ? "已启用" : "已停用"}
              {"allowed_tools" in item && <small>{item.allowed_tools.length} 个工具已授权</small>}</span><ChevronRight size={16} />
          </Link>)}
          {!items?.length && <p className="extension-empty">{query || status ? "没有符合条件的扩展" : tab === "mcp" ? "还没有 MCP 连接" : source === "platform" ? "暂无平台 Skill" : "还没有个人 Skill"}</p>}
        </div>}
      </Tabs.Root>
    </div>
  </>;
}
