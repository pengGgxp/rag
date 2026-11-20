"use client";
import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, ChevronRight, Download, FileText, FolderOpen, Loader2, MoreHorizontal, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import * as Tabs from "../../components/ui/tabs";
import { WorkspaceSkeleton } from "../workspace-header";
import { useExtensionResource } from "./extension-data";
import { skillCatalog, filterSkills, type SkillSummary, type CatalogSkill } from "./skill-catalog";
import { downloadFile, request } from "../api";
import { Modal } from "../modal";

export function SkillCatalogView({ query, status, returnTo, filter }: { query: string; status: string; returnTo: string; filter: (values: Record<string, string>) => void }) {
  const router = useRouter();
  const canWrite = true;
  const [installing, setInstalling] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [feedback, setFeedback] = useState({ error: "", message: "" });
  const [menu, setMenu] = useState<{ item: CatalogSkill; anchor: HTMLElement } | null>(null);
  const [locations, setLocations] = useState<{ item: CatalogSkill; action: "view" | "edit" | "uninstall" | "locate" } | null>(null);
  const own = useExtensionResource<SkillSummary[]>("/agent/skills");
  const packages = useExtensionResource<SkillSummary[]>("/agent/skills/platform");
  const catalog = skillCatalog(packages.data || [], own.data || []);
  const items = filterSkills(catalog, query, status);
  const error = own.error || packages.error, loading = own.loading || packages.loading;
  async function reload() { await Promise.all([own.reload(), packages.reload()]); }
  const href = (id: string, platform = false, action = "view") => `/extensions/skills/${id}?${platform ? "platform=1&" : ""}${action !== "view" ? `action=${action}&` : ""}returnTo=${encodeURIComponent(returnTo)}`;
  function open(item: CatalogSkill, action: "view" | "edit" | "uninstall" | "locate") {
    setMenu(null);
    if (item.installations.length > 1) { setLocations({ item, action }); return; }
    const current = item.installations[0];
    router.push(action === "locate" && current ? `/resources?path=${encodeURIComponent(current.directory || "")}` : href(current?.id || item.packageId!, !current, action));
  }
  async function install(item: CatalogSkill) {
    if (installing || !item.packageId) return;
    setInstalling(item.key); setFeedback({ error: "", message: "" });
    try {
      await request(`/agent/skills/platform/${item.packageId}/install`, { method: "POST", headers: { "X-Request-ID": crypto.randomUUID() } });
      await reload(); setFeedback({ error: "", message: `已安装 ${item.name}` });
    } catch (cause) { setFeedback({ error: cause instanceof Error ? cause.message : "安装失败，请重试", message: "" }); }
    finally { setInstalling(null); }
  }
  async function download(item: CatalogSkill) {
    if (!item.packageId) return;
    setDownloading(true); setFeedback({ error: "", message: "" });
    try { await downloadFile(`/agent/skills/platform/${item.packageId}/download`, `${item.name}.zip`); setMenu(null); }
    catch (cause) { setMenu(null); setFeedback({ error: cause instanceof Error ? cause.message : "下载失败，请重试", message: "" }); }
    finally { setDownloading(false); }
  }
  return <>
    <div className="extension-toolbar skill-catalog-toolbar">
      <label className="extension-search"><Search size={17} /><input aria-label="搜索 Skill" placeholder="搜索名称或用途" value={query} onChange={e => filter({ q: e.target.value })} /></label>
      <Tabs.Root value={status === "installed" || status === "available" ? status : ""} onValueChange={value => filter({ status: value })}><Tabs.List className="skill-filters" aria-label="安装状态"><Tabs.Trigger value="">全部</Tabs.Trigger><Tabs.Trigger value="installed">已安装{own.data && <span>{catalog.filter(item => item.installations.length).length}</span>}</Tabs.Trigger><Tabs.Trigger value="available">未安装</Tabs.Trigger></Tabs.List></Tabs.Root>
      <button className="icon-button skill-refresh" aria-label="刷新" disabled={loading || !!installing} onClick={() => void reload()}><RefreshCw size={17} /></button>
    </div>
    {feedback.error && <p role="alert" className="extension-error skill-catalog-feedback">{feedback.error}<button className="secondary" onClick={() => setFeedback({ error: "", message: "" })}>关闭提示</button></p>}
    {feedback.message && <p role="status" className="skill-catalog-feedback">{feedback.message}</p>}
    {loading ? <WorkspaceSkeleton /> : error ? <div role="alert" className="extension-error">{error}<button className="secondary" onClick={() => void reload()}>重试</button></div> : <>
      <div className="skill-results-heading"><h2>{status === "installed" ? "已安装的 Skill" : status === "available" ? "发现 Skill" : "全部 Skill"}</h2><span>{items.length} 个 Skill</span></div>
      <div className="skill-grid">{items.map(item => <article className="skill-card" key={item.key}>
        <span className="extension-symbol skill-card-icon"><FileText size={23} strokeWidth={1.7} /></span>
        <div className="skill-card-copy"><button className="skill-card-title" onClick={() => open(item, "view")}>{item.name}</button><p>{item.description || (item.needsRepair ? "清单需要修复，打开后编辑 SKILL.md" : "暂无简介")}</p></div>
        {item.installations.length ? <button id={`skill-more-${item.installations[0].id}`} className="icon-button skill-card-action" aria-label={`${item.name} 的更多操作`} aria-haspopup="dialog" onClick={event => setMenu({ item, anchor: event.currentTarget })}><MoreHorizontal size={19} /></button>
        : <button className="icon-button skill-card-action" aria-label={`安装 ${item.name}`} title={canWrite ? "安装到我的资源" : "当前账号无权安装"} disabled={!canWrite || !!installing} onClick={() => void install(item)}>{installing === item.key ? <Loader2 size={18} className="spin" /> : <Plus size={19} />}</button>}
        <div className="skill-card-footer"><span className={item.installations.length ? "skill-installed" : ""}>{item.installations.length ? <><Check size={13} />已安装{item.installations.length > 1 ? ` · ${item.installations.length} 个位置` : ""}</> : "未安装"}</span>{item.needsRepair && <span className="skill-repair">需要修复</span>}</div>
      </article>)}</div>
      {!items.length && <div className="extension-empty"><FileText size={28} /><p>{query ? "没有找到匹配的 Skill" : status === "installed" ? "还没有安装 Skill" : "暂无可用的 Skill"}</p>{(query || status) && <button className="secondary" onClick={() => filter({ q: "", status: "" })}>查看全部 Skill</button>}</div>}
    </>}
    {menu && <Modal title={menu.item.name} anchor={menu.anchor} busy={downloading} onClose={() => setMenu(null)} restoreFocusSelector={`#skill-more-${menu.item.installations[0].id}`}><div className="skill-action-menu" role="menu" aria-label="Skill 操作" onKeyDown={event => { if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return; event.preventDefault(); const entries = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')); const index = entries.indexOf(document.activeElement as HTMLButtonElement); entries[event.key === "Home" ? 0 : event.key === "End" ? entries.length - 1 : (index + (event.key === "ArrowUp" ? -1 : 1) + entries.length) % entries.length]?.focus(); }}>
      <button autoFocus role="menuitem" onClick={() => open(menu.item, "view")}><FileText size={16} />查看详情</button>
      {canWrite && <button role="menuitem" onClick={() => open(menu.item, "edit")}><Pencil size={16} />编辑 Skill</button>}
      <button role="menuitem" onClick={() => open(menu.item, "locate")}><FolderOpen size={16} />在我的资源中查看</button>
      {menu.item.packageId && <button role="menuitem" disabled={downloading} onClick={() => void download(menu.item)}>{downloading ? <Loader2 size={16} className="spin" /> : <Download size={16} />}{downloading ? "正在下载" : "下载仓库包"}</button>}
      {canWrite && <button role="menuitem" className="skill-uninstall" onClick={() => open(menu.item, "uninstall")}><Trash2 size={16} />卸载</button>}
    </div></Modal>}
    {locations && <Modal title="选择安装位置" onClose={() => setLocations(null)} restoreFocusSelector={`#skill-more-${locations.item.installations[0].id}`}><div className="skill-detail-confirm"><p>此 Skill 有多个安装位置，请选择要操作的目录。</p><ul className="skill-detail-directory-list">{locations.item.installations.map(item => <li key={item.id}><Link href={locations.action === "locate" ? `/resources?path=${encodeURIComponent(item.directory || "")}` : href(item.id, false, locations.action)}><FolderOpen size={18} /><span>{item.directory}{item.manifest_error && <small> · 需要修复</small>}</span><ChevronRight size={16} /></Link></li>)}</ul></div></Modal>}
  </>;
}
