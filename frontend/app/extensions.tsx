"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  Check, CheckCircle2, CircleAlert, Download, FileCode2, FolderUp,
  Loader2, Pencil, Plug, Plus, Puzzle, RefreshCw, Search, Trash2, Upload,
} from "lucide-react";
import * as Tabs from "../components/ui/tabs";
import { WorkspaceHeader } from "./workspace-header";
import { downloadFile, request } from "./api";
import { Modal } from "./modal";
import { SkillBundleEditor } from "./skill-bundle-editor";
import "./extensions.css";

export type Skill = {
  id: string; name: string; description: string; enabled: boolean; markdown?: string;
  files?: SkillFile[];
};
export type SkillFile = { path: string; size: number; media_type: string; text: boolean; sha256: string };
export type MCPConnection = {
  id: string; name: string; url: string; transport: "streamable_http" | "sse";
  enabled: boolean; has_credentials: boolean; header_names: string[];
  tools: { name: string; description?: string; inputSchema: object }[];
  allowed_tools: string[]; last_checked_at: string | null;
};
export type ExtensionSelection = { skill_ids: string[]; mcp_server_ids: string[] };
const json = (body: unknown, method = "POST") => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
const failure = (reason: unknown) => reason instanceof Error ? reason.message : "操作失败，请重试";

export function AgentExtensionPicker({
  selection, disabled, onChange, onManage,
}: {
  selection: ExtensionSelection; disabled: boolean;
  onChange: (value: ExtensionSelection) => Promise<void>; onManage: () => void;
}) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<MCPConnection[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const refresh = async () => {
    setLoading(true);
    setError("");
    try {
      const [s, m] = await Promise.all([
        request<Skill[]>("/agent/skills"), request<MCPConnection[]>("/agent/mcp"),
      ]);
      setSkills(s); setServers(m);
      setLoaded(true);
    } catch (reason) { setError(failure(reason)); }
    finally { setLoading(false); }
  };
  const toggle = async (key: keyof ExtensionSelection, id: string) => {
    setLoading(true); setError("");
    try {
      await onChange({
        ...selection,
        [key]: selection[key].includes(id) ? selection[key].filter(item => item !== id) : [...selection[key], id],
      });
    } catch (reason) { setError(failure(reason)); }
    finally { setLoading(false); }
  };
  const count = selection.skill_ids.length + selection.mcp_server_ids.length;
  const available: ExtensionSelection = {
    skill_ids: selection.skill_ids.filter(id => skills.some(item => item.id === id && item.enabled)),
    mcp_server_ids: selection.mcp_server_ids.filter(id => servers.some(item => item.id === id && item.enabled && item.allowed_tools.length)),
  };
  const unavailable = count - available.skill_ids.length - available.mcp_server_ids.length;
  return (
    <details className="extension-picker" onToggle={(event) => {
      if (event.currentTarget.open) void refresh();
    }}>
      <summary><Puzzle size={15} />扩展{count > 0 && <span>{count}</span>}</summary>
      <div className="extension-picker-menu">
        {error && <p role="alert" className="form-error">{error}</p>}
        {loading && <Loader2 size={16} className="spin" aria-label="正在加载扩展" />}
        {loaded && unavailable > 0 && <div className="extension-unavailable">
          <p>{unavailable} 个已选扩展已停用、删除或撤销授权</p>
          <button className="text-button" disabled={disabled || loading} onClick={async () => {
            setLoading(true); setError("");
            try { await onChange(available); }
            catch (reason) { setError(failure(reason)); }
            finally { setLoading(false); }
          }}><Trash2 size={14} />移除不可用扩展</button>
        </div>}
        <div className="extension-picker-heading">Skills</div>
        {skills.filter(item => item.enabled).map(skill => (
          <label key={skill.id} title={skill.description}>
            <input type="checkbox" disabled={disabled || loading}
              checked={selection.skill_ids.includes(skill.id)}
              onChange={() => void toggle("skill_ids", skill.id)} />
            <FileCode2 size={15} /><span>{skill.name}</span>
          </label>
        ))}
        {!skills.some(item => item.enabled) && <p className="muted">暂无启用的 Skill</p>}
        <div className="extension-picker-heading">MCP</div>
        {servers.filter(item => item.enabled).map(server => (
          <label key={server.id} title={server.url}>
            <input type="checkbox" disabled={disabled || loading || !server.allowed_tools.length}
              checked={selection.mcp_server_ids.includes(server.id)}
              onChange={() => void toggle("mcp_server_ids", server.id)} />
            <Plug size={15} /><span>{server.name}</span><small>{server.allowed_tools.length} 个工具</small>
          </label>
        ))}
        {!servers.some(item => item.enabled) && <p className="muted">暂无启用的 MCP</p>}
        <button className="text-button" onClick={onManage}><Puzzle size={14} />管理扩展</button>
      </div>
    </details>
  );
}

export function AgentExtensions() {
  const [tab, setTab] = useState("mcp");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<MCPConnection[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editor, setEditor] = useState<
    { kind: "skill"; data?: Skill } | { kind: "mcp"; data?: MCPConnection } | null
  >(null);
  const [deleting, setDeleting] = useState<{ kind: "skill" | "mcp"; id: string; name: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const reload = useCallback(async () => {
    const [s, m] = await Promise.all([
      request<Skill[]>("/agent/skills"), request<MCPConnection[]>("/agent/mcp"),
    ]);
    setSkills(s); setServers(m);
  }, []);
  useEffect(() => { void reload().catch(e => setError(failure(e))).finally(() => setLoading(false)); }, [reload]);
  const action = async (run: () => Promise<unknown>, message = "") => {
    setBusy(true); setError(""); setNotice("");
    try { await run(); await reload(); setNotice(message); }
    catch (reason) { setError(failure(reason)); }
    finally { setBusy(false); }
  };
  const editSkill = async (skill: Skill) => {
    await action(async () => {
      setEditor({ kind: "skill", data: await request<Skill>(`/agent/skills/${skill.id}`) });
    });
  };
  const importSkill = async (files: File[], folder = false) => {
    await action(async () => {
      const data = new FormData();
      files.forEach(file => {
        data.append("files", file);
        if (folder) data.append("paths", file.webkitRelativePath);
        else if (files.length === 1 && !file.name.toLowerCase().endsWith(".zip")) data.append("paths", "SKILL.md");
      });
      const imported = await request<Skill>("/agent/skills/import", { method: "POST", body: data });
      setEditor({ kind: "skill", data: imported });
    }, "Skill 已导入");
  };
  const visibleServers = servers.filter(item => `${item.name} ${item.url}`.toLowerCase().includes(search.toLowerCase()));
  const visibleSkills = skills.filter(item => `${item.name} ${item.description}`.toLowerCase().includes(search.toLowerCase()));
  return (
    <>
      <WorkspaceHeader title="Agent 扩展" />
      <div className="workspace-body">
        <Tabs.Root value={tab} onValueChange={(value) => { setTab(value); setSearch(""); setError(""); setNotice(""); }}>
          <div className="extensions-tabs">
            <Tabs.List className="tab-list" aria-label="扩展类型">
              <Tabs.Trigger value="mcp"><Plug size={16} />MCP<span>{servers.length}</span></Tabs.Trigger>
              <Tabs.Trigger value="skill"><FileCode2 size={16} />Skills<span>{skills.length}</span></Tabs.Trigger>
            </Tabs.List>
          </div>
          <section className="extensions-content">
            <div className="extensions-toolbar">
              <label className="search-field"><Search size={15} /><input aria-label="搜索扩展" placeholder="搜索扩展" value={search} onChange={e => setSearch(e.target.value)} /></label>
              <div>
                <button className="icon-button" title="刷新" aria-label="刷新扩展" disabled={busy} onClick={() => void action(reload)}><RefreshCw size={16} /></button>
                {tab === "skill" && <>
                  <button className="icon-button" title="导入 Skill 文件夹" aria-label="导入 Skill 文件夹" disabled={busy} onClick={() => folderInput.current?.click()}><FolderUp size={16} /></button>
                  <button className="secondary" disabled={busy} onClick={() => fileInput.current?.click()}><Upload size={15} />导入 Skill</button>
                </>}
                <button className="primary" disabled={busy} onClick={() => setEditor(tab === "mcp" ? { kind: "mcp" } : { kind: "skill" })}><Plus size={16} />{tab === "mcp" ? "添加 MCP" : "创建 Skill"}</button>
              </div>
            </div>
            <input ref={fileInput} type="file" accept=".zip,.md" hidden onChange={e => {
              const files = Array.from(e.currentTarget.files ?? []);
              e.currentTarget.value = "";
              if (files.length) void importSkill(files);
            }} />
            <input ref={folderInput} type="file" multiple {...{ webkitdirectory: "", directory: "" }} hidden onChange={e => {
              const files = Array.from(e.currentTarget.files ?? []);
              e.currentTarget.value = "";
              if (files.length) void importSkill(files, true);
            }} />
            {error && <p className="extensions-alert" role="alert"><CircleAlert size={16} />{error}</p>}
            {notice && <p className="extensions-success" role="status"><CheckCircle2 size={16} />{notice}</p>}
            {loading ? <div className="extensions-empty"><Loader2 className="spin" size={24} />正在加载扩展…</div> : <>
              <Tabs.Content value="mcp">
                {visibleServers.map(server => (
                  <div className="extension-row" key={server.id}>
                    <span className="extension-symbol mcp"><Plug size={21} /></span>
                    <div className="extension-copy">
                      <div><strong>{server.name}</strong><span className={`extension-status ${server.enabled ? "active" : ""}`}>{server.enabled ? "已启用" : "已停用"}</span></div>
                      <p title={server.url}>{server.url}</p>
                      <small>{server.transport === "sse" ? "SSE" : "Streamable HTTP"} · {server.allowed_tools.length}/{server.tools.length} 个工具已授权{server.has_credentials ? " · 已配置认证" : ""}</small>
                    </div>
                    <div className="extension-row-actions">
                      <label className="extension-enable" title={server.enabled ? "停用连接" : "启用连接"}><input type="checkbox" aria-label={`${server.enabled ? "停用" : "启用"} ${server.name}`} checked={server.enabled} disabled={busy} onChange={() => void action(() => request(`/agent/mcp/${server.id}`, json({ ...serverPayload(server), enabled: !server.enabled }, "PUT")))} /></label>
                      <button className="icon-button" title="测试并同步工具" aria-label={`同步 ${server.name}`} disabled={busy} onClick={() => void action(() => request(`/agent/mcp/${server.id}/sync`, { method: "POST" }), "连接成功，工具已同步；新工具需要授权")}><RefreshCw size={16} /></button>
                      <button className="icon-button" title="编辑与工具授权" aria-label={`编辑 ${server.name}`} disabled={busy} onClick={() => setEditor({ kind: "mcp", data: server })}><Pencil size={16} /></button>
                      <button className="icon-button" title="删除连接" aria-label={`删除 ${server.name}`} disabled={busy} onClick={() => setDeleting({ kind: "mcp", id: server.id, name: server.name })}><Trash2 size={16} /></button>
                    </div>
                  </div>
                ))}
                {!visibleServers.length && <div className="extensions-empty"><Plug size={28} /><h2>{search ? "未找到连接" : "还没有 MCP 连接"}</h2></div>}
              </Tabs.Content>
              <Tabs.Content value="skill">
                {visibleSkills.map(skill => (
                  <div className="extension-row" key={skill.id}>
                    <span className="extension-symbol skill"><FileCode2 size={21} /></span>
                    <div className="extension-copy"><div><strong>{skill.name}</strong><span className={`extension-status ${skill.enabled ? "active" : ""}`}>{skill.enabled ? "已启用" : "已停用"}</span></div><p>{skill.description}</p></div>
                    <div className="extension-row-actions">
                      <label className="extension-enable" title={skill.enabled ? "停用 Skill" : "启用 Skill"}><input type="checkbox" aria-label={`${skill.enabled ? "停用" : "启用"} ${skill.name}`} checked={skill.enabled} disabled={busy} onChange={() => void action(async () => {
                        const full = await request<Skill>(`/agent/skills/${skill.id}`);
                        await request(`/agent/skills/${skill.id}`, json({ markdown: full.markdown, enabled: !skill.enabled }, "PUT"));
                      })} /></label>
                      <button className="icon-button" title="导出 Skill ZIP" aria-label={`导出 ${skill.name}`} disabled={busy} onClick={() => void action(() => downloadFile(`/agent/skills/${skill.id}/export`, `${skill.name}.zip`))}><Download size={16} /></button>
                      <button className="icon-button" title="编辑 Skill" aria-label={`编辑 ${skill.name}`} disabled={busy} onClick={() => void editSkill(skill)}><Pencil size={16} /></button>
                      <button className="icon-button" title="删除 Skill" aria-label={`删除 ${skill.name}`} disabled={busy} onClick={() => setDeleting({ kind: "skill", id: skill.id, name: skill.name })}><Trash2 size={16} /></button>
                    </div>
                  </div>
                ))}
                {!visibleSkills.length && <div className="extensions-empty"><FileCode2 size={28} /><h2>{search ? "未找到 Skill" : "还没有 Skill"}</h2></div>}
              </Tabs.Content>
            </>}
          </section>
        </Tabs.Root>
      </div>
      {editor?.kind === "skill" && <SkillBundleEditor key={editor.data?.id ?? "new"} skill={editor.data} onClose={() => setEditor(null)} onSaved={async () => { await reload(); setEditor(null); setNotice("已保存"); }} />}
      {editor?.kind === "mcp" && <ExtensionEditor key={editor.data?.id ?? "new"} editor={editor} onClose={() => setEditor(null)} onSaved={async () => { await reload(); setEditor(null); setNotice("已保存"); }} />}
      {deleting && <Modal title="删除扩展" busy={busy} onClose={() => setDeleting(null)}>
        <div className="extension-delete">
        <p>确定删除「{deleting.name}」？后续对话将无法调用此扩展，已有回答不受影响。</p>
        <div className="modal-footer"><button className="secondary" disabled={busy} onClick={() => setDeleting(null)}>取消</button><button className="primary" disabled={busy} onClick={() => void action(async () => {
          await request(`/agent/${deleting.kind === "skill" ? "skills" : "mcp"}/${deleting.id}`, { method: "DELETE" });
          setDeleting(null);
        }, "已删除")}><Trash2 size={15} />删除</button></div>
        </div>
      </Modal>}
    </>
  );
}

function serverPayload(server: MCPConnection) {
  return { name: server.name, url: server.url, transport: server.transport, enabled: server.enabled, allowed_tools: server.allowed_tools };
}

function ExtensionEditor({ editor, onClose, onSaved }: {
  editor: { kind: "mcp"; data?: MCPConnection };
  onClose: () => void; onSaved: () => Promise<void>;
}) {
  const server = editor.data;
  const [name, setName] = useState(server?.name ?? "");
  const [url, setUrl] = useState(server?.url ?? "");
  const [transport, setTransport] = useState(server?.transport ?? "streamable_http");
  const [headers, setHeaders] = useState("");
  const [clearHeaders, setClearHeaders] = useState(false);
  const [allowed, setAllowed] = useState(server?.allowed_tools ?? []);
  const [acknowledged, setAcknowledged] = useState(false);
  const [enabled, setEnabled] = useState(editor.data?.enabled ?? true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const addressChanged = !!server && (url !== server.url || transport !== server.transport);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      let parsed: Record<string, string> | undefined;
      if (clearHeaders) parsed = {};
      else if (headers.trim()) {
        try { parsed = JSON.parse(headers); } catch { throw new Error("认证请求头必须是 JSON 对象"); }
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.values(parsed).some(value => typeof value !== "string"))
          throw new Error("认证请求头必须是名称与字符串值组成的 JSON 对象");
      }
      const payload = { name, url, transport, enabled, allowed_tools: addressChanged ? [] : allowed, acknowledge_external_access: acknowledged, ...(parsed !== undefined ? { headers: parsed } : {}) };
      await request(`/agent/mcp${editor.data ? `/${editor.data.id}` : ""}`, json(payload, editor.data ? "PUT" : "POST"));
      setHeaders("");
      await onSaved();
    } catch (reason) { setError(failure(reason)); }
    finally { setBusy(false); }
  };
  return <Modal title={editor.data ? "编辑 MCP" : "添加 MCP"} wide busy={busy} onClose={onClose}>
    <form className="extension-form" onSubmit={submit}>
        <div className="extension-form-grid"><label>连接名称<input value={name} onChange={e => setName(e.target.value)} required maxLength={200} /></label><label>传输协议<select value={transport} onChange={e => setTransport(e.target.value as "sse" | "streamable_http")}><option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option></select></label></div>
        <label>服务地址<input type="url" placeholder="https://example.com/mcp" value={url} onChange={e => setUrl(e.target.value)} required /></label>
        <label>认证请求头<input type="password" autoComplete="new-password" value={headers} disabled={clearHeaders} onChange={e => setHeaders(e.target.value)} placeholder={server?.has_credentials ? `已保存 ${server.header_names.join("、")}；留空保留` : '{"Authorization":"Bearer ..."}'} /></label>
        {server?.has_credentials && <label className="extension-checkbox"><input type="checkbox" checked={clearHeaders} onChange={e => setClearHeaders(e.target.checked)} />移除已保存的认证信息</label>}
        {addressChanged && <p className="extensions-alert"><CircleAlert size={16} />地址变化后需要重新同步并授权工具。</p>}
        {!!server?.tools.length && !addressChanged && <fieldset className="extension-tools"><legend>工具授权</legend>{server.tools.map(tool => <label key={tool.name}><input type="checkbox" checked={allowed.includes(tool.name)} onChange={() => setAllowed(items => items.includes(tool.name) ? items.filter(item => item !== tool.name) : [...items, tool.name])} /><span><strong>{tool.name}</strong><small>{tool.description || "未提供描述"}</small></span></label>)}</fieldset>}
        {allowed.some(item => !server?.allowed_tools.includes(item)) && !addressChanged && <label className="extension-checkbox extension-consent"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} required />我授权向此服务发送调用参数，并执行所选工具（可能修改外部数据）。</label>}
      <label className="extension-checkbox"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用</label>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="modal-footer"><button className="secondary" type="button" disabled={busy} onClick={onClose}>取消</button><button className="primary" type="submit" disabled={busy}>{busy ? <Loader2 size={15} className="spin" /> : <Check size={15} />}保存</button></div>
    </form>
  </Modal>;
}
