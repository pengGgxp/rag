"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Loader2, Puzzle } from "lucide-react";
import { Dialog } from "../../components/ui/dialog";
import { Tooltip } from "../../components/ui/tooltip";
import { request } from "../api";
import type { MCPConnection, PersonalSkill, ExtensionSelection } from "./extension-data";
import "./extensions.css";

export function ExtensionPicker({ selection, disabled, supportsTools, onChange }: {
  selection: ExtensionSelection | null; disabled: boolean; supportsTools: boolean;
  onChange: (value: ExtensionSelection) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<PersonalSkill[]>([]);
  const [platform, setPlatform] = useState<PersonalSkill[]>([]);
  const [servers, setServers] = useState<MCPConnection[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const anchor = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError("");
    Promise.all([request<PersonalSkill[]>("/agent/skills", { signal: controller.signal }),
      request<PersonalSkill[]>("/agent/skills/platform", { signal: controller.signal }),
      request<MCPConnection[]>("/agent/mcp", { signal: controller.signal })])
      .then(([s, p, m]) => { if (!controller.signal.aborted) { setSkills(s); setPlatform(p); setServers(m); } })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "加载失败"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, reload]);
  const effective = selection ?? { skill_ids: skills.filter(item => item.enabled).map(item => item.id),
    mcp_server_ids: servers.filter(item => item.enabled && item.allowed_tools.length).map(item => item.id) };
  const unavailable = effective.skill_ids.filter(id => !skills.some(item => item.id === id && item.enabled)).length
    + effective.mcp_server_ids.filter(id => !servers.some(item => item.id === id && item.enabled && item.allowed_tools.length)).length;
  async function change(next: ExtensionSelection) {
    setSaving(true); setError("");
    try { await onChange(next); } catch (e) { setError(e instanceof Error ? e.message : "选择未保存"); }
    finally { setSaving(false); }
  }
  return <>
    <Tooltip content={supportsTools ? "选择对话扩展" : "当前模型无法使用扩展工具"}>
      <button ref={anchor} className="icon-button" type="button" aria-label="对话扩展" aria-expanded={open} aria-haspopup="dialog"
        disabled={disabled} onClick={() => setOpen(value => !value)}><Puzzle size={17} /></button>
    </Tooltip>
    {open && <Dialog title="对话扩展" anchor={anchor.current} onClose={() => setOpen(false)}>
      <div className="extension-picker-body">
        {!supportsTools && <p>当前模型无法使用扩展工具，选择支持工具调用的模型后即可使用。</p>}
        {loading ? <Loader2 size={18} className="spin" aria-label="正在加载扩展" /> : error ? <div role="alert"><p>{error}</p><button type="button" className="secondary" onClick={() => setReload(value => value + 1)}>重试</button></div> : <>
          <h3>平台 Skill · 自动发现</h3>{platform.map(item => <p key={item.id}>{item.name}</p>)}{!platform.length && <p>暂无平台 Skill</p>}
          <h3>我的 Skill</h3>{skills.filter(item => item.enabled).map(item => <label className="extension-check" key={item.id}>
            <input type="checkbox" disabled={saving || !supportsTools} checked={effective.skill_ids.includes(item.id)} onChange={e => void change({ ...effective,
              skill_ids: e.target.checked ? [...effective.skill_ids, item.id] : effective.skill_ids.filter(id => id !== item.id) })} />{item.name}
          </label>)}{!skills.some(item => item.enabled) && <p>暂无启用的个人 Skill</p>}
          <h3>我的 MCP</h3>{servers.filter(item => item.enabled && item.allowed_tools.length).map(item => <label className="extension-check" key={item.id}>
            <input type="checkbox" disabled={saving || !supportsTools} checked={effective.mcp_server_ids.includes(item.id)} onChange={e => void change({ ...effective,
              mcp_server_ids: e.target.checked ? [...effective.mcp_server_ids, item.id] : effective.mcp_server_ids.filter(id => id !== item.id) })} />{item.name}
          </label>)}{!servers.some(item => item.enabled && item.allowed_tools.length) && <p>暂无已授权的 MCP</p>}
          {!!unavailable && <p>{unavailable} 个已选扩展不可用 <button type="button" className="text-button" disabled={saving} onClick={() => void change({
            skill_ids: effective.skill_ids.filter(id => skills.some(item => item.id === id && item.enabled)),
            mcp_server_ids: effective.mcp_server_ids.filter(id => servers.some(item => item.id === id && item.enabled && item.allowed_tools.length)),
          })}>移除</button></p>}
        </>}
        <Link href="/extensions"><Puzzle size={15} />管理扩展</Link>
      </div>
    </Dialog>}
  </>;
}
