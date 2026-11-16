"use client";
import { use, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Loader2, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { ApiError, request } from "../../../api";
import { WorkspaceHeader, WorkspaceSkeleton } from "../../../workspace-header";
import { useAdminChanges } from "../../../admin/admin-changes";
import { Pagination } from "../../../admin/admin-ui";
import { json, useExtensionResource, type MCPConnection } from "../../extension-data";

type Form = { name: string; url: string; transport: "streamable_http" | "sse"; enabled: boolean; allowed_tools: string[] };
const empty: Form = { name: "", url: "", transport: "streamable_http", enabled: false, allowed_tools: [] };
const draftOf = (row: MCPConnection): Form => ({ name: row.name, url: row.url, transport: row.transport, enabled: row.enabled, allowed_tools: row.allowed_tools });
export default function MCPPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const paramsQuery = useSearchParams();
  const resource = useExtensionResource<MCPConnection>(id === "new" ? null : `/agent/mcp/${id}`);
  const [connection, setConnection] = useState<MCPConnection | null>(null);
  const [form, setForm] = useState<Form>(empty);
  const [saved, setSaved] = useState<Form>(empty);
  const [auth, setAuth] = useState("keep");
  const [token, setToken] = useState("");
  const [headers, setHeaders] = useState([{ name: "", value: "" }]);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [toolQuery, setToolQuery] = useState("");
  const [toolPage, setToolPage] = useState(1);
  const changedConnection = form.url !== saved.url || form.transport !== saved.transport || auth !== "keep";
  const dirty = JSON.stringify(form) !== JSON.stringify(saved) || auth !== "keep";
  useAdminChanges(dirty, busy);
  function accept(row: MCPConnection) {
    setConnection(row); const next = draftOf(row); setForm(next); setSaved(next);
    setToken(""); setHeaders([{ name: "", value: "" }]); setAuth("keep"); setAck(false);
  }
  useEffect(() => { if (resource.data) accept(resource.data); }, [resource.data]);
  function payload(draft = false) {
    let authentication: Record<string, string> | undefined;
    if (auth === "none") authentication = {};
    if (auth === "token") { if (!token.trim()) throw new Error("请填写 Token"); authentication = { Authorization: `Bearer ${token.trim()}` }; }
    if (auth === "custom") {
      authentication = {};
      for (const header of headers) {
        if (!header.name.trim() || !header.value.trim()) throw new Error("请完整填写请求头名称和值");
        if (Object.keys(authentication).some(name => name.toLowerCase() === header.name.trim().toLowerCase())) throw new Error("请求头名称不能重复");
        authentication[header.name.trim()] = header.value;
      }
    }
    return { ...form, enabled: draft || changedConnection ? false : form.enabled,
      allowed_tools: changedConnection ? [] : form.allowed_tools, acknowledge_external_access: ack,
      ...(authentication === undefined ? {} : { headers: authentication }), ...(connection ? { version: connection.version } : {}) };
  }
  async function save(discover = false) {
    setBusy(true); setError(""); setNotice("");
    let savedId = connection?.id;
    try {
      let row = connection;
      if (!row || dirty) row = await request<MCPConnection>(row ? `/agent/mcp/${row.id}` : "/agent/mcp",
        json(payload(discover), row ? "PUT" : "POST"));
      accept(row);
      savedId = row.id;
      if (discover) {
        const synced = await request<MCPConnection>(`/agent/mcp/${row.id}/sync`, json({ version: row.version }));
        accept(synced); setNotice(synced.tools.length ? "连接成功，请选择允许使用的工具" : "连接成功，服务尚未提供工具");
      } else setNotice("已保存");
      setConflict(false);
      if (id === "new") router.replace(`/extensions/mcp/${row.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
      setConflict(e instanceof ApiError && e.status === 409);
      if (id === "new" && savedId) router.replace(`/extensions/mcp/${savedId}?discovery=failed`);
    } finally { setBusy(false); }
  }
  const tools = !changedConnection ? connection?.tools ?? [] : [];
  const filteredTools = tools.filter(tool => `${tool.name} ${tool.description ?? ""}`.toLocaleLowerCase().includes(toolQuery.trim().toLocaleLowerCase()));
  const pageSize = 20;
  const currentPage = Math.min(toolPage, Math.max(1, Math.ceil(filteredTools.length / pageSize)));
  const visibleTools = filteredTools.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  return <>
    <WorkspaceHeader title={id === "new" ? "添加 MCP" : saved.name || "编辑 MCP"} />
    <div className="workspace-body extension-page"><Link href="/extensions?tab=mcp" className="extension-back"><ArrowLeft size={16} />MCP</Link>
      {resource.loading && id !== "new" ? <WorkspaceSkeleton /> : resource.error ? <div role="alert" className="extension-error">{resource.error}<button className="secondary" onClick={() => void resource.reload()}>重试</button></div> :
      <form className="extension-form" onSubmit={e => { e.preventDefault(); void save(); }}>
        <fieldset className="extension-fields" disabled={busy}>
        <div className="extension-form-grid">
          <label>连接名称<input required maxLength={200} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
          <label>服务地址<input required type="url" placeholder="https://example.com/mcp" value={form.url} onChange={e => { setForm({ ...form, url: e.target.value }); setAck(false); }} /></label>
        </div>
        <section>
          <label>认证方式<select value={auth} onChange={e => { setAuth(e.target.value); setAck(false); }}>
            <option value="keep">{connection?.has_credentials ? "保留已配置的认证" : "无认证"}</option>
            {connection?.has_credentials && <option value="none">清除认证</option>}<option value="token">Bearer Token</option><option value="custom">自定义请求头</option>
          </select></label>
          {auth === "token" && <label>Token<input type="password" autoComplete="new-password" value={token} onChange={e => setToken(e.target.value)} /></label>}
          {auth === "custom" && <div className="extension-section">{headers.map((header, index) => <div className="extension-header-field" key={index}>
            <input aria-label={`请求头名称 ${index + 1}`} placeholder="请求头名称" value={header.name} onChange={e => setHeaders(headers.map((item, i) => i === index ? { ...item, name: e.target.value } : item))} />
            <input aria-label={`请求头值 ${index + 1}`} type="password" autoComplete="new-password" placeholder="值" value={header.value} onChange={e => setHeaders(headers.map((item, i) => i === index ? { ...item, value: e.target.value } : item))} />
            <button className="icon-button" type="button" aria-label={`移除请求头 ${index + 1}`} onClick={() => setHeaders(headers.filter((_, i) => i !== index))}><Trash2 size={16} /></button>
          </div>)}<button className="secondary" type="button" onClick={() => setHeaders([...headers, { name: "", value: "" }])}><Plus size={16} />添加请求头</button></div>}
          {auth === "none" && <p className="extension-warning">保存后将清除认证，并要求重新测试和授权。</p>}
        </section>
        <details><summary>高级设置</summary><label>传输协议<select value={form.transport} onChange={e => { setForm({ ...form, transport: e.target.value as Form["transport"] }); setAck(false); }}>
          <option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option></select></label></details>
        <section className="extension-section">
          <div className="extension-actions"><h2>工具授权</h2><button className="secondary" type="button" disabled={busy || conflict || !form.name.trim() || !form.url.trim()} onClick={() => void save(true)}>{busy ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}测试并获取工具</button></div>
          {changedConnection && connection && <p className="extension-warning">连接已更改，测试后请重新选择工具。</p>}
          {!!tools.length && <div className="extension-tool-search">
            <label>搜索工具<input type="search" value={toolQuery} onChange={e => { setToolQuery(e.target.value); setToolPage(1); }} /></label>
            <span className="muted">已选择 {form.allowed_tools.length} 项</span>
          </div>}
          {visibleTools.map(tool => <label className="extension-check extension-tool" key={tool.name}>
            <input type="checkbox" checked={form.allowed_tools.includes(tool.name)} onChange={e => { setForm({ ...form, allowed_tools: e.target.checked ? [...form.allowed_tools, tool.name] : form.allowed_tools.filter(name => name !== tool.name) }); setAck(false); }} />
            <span><strong>{tool.name}</strong><small>{tool.description || "未提供用途说明"}</small></span>
          </label>)}
          {!!tools.length && !filteredTools.length && <p className="extension-warning">未找到匹配工具</p>}
          {!!tools.length && <Pagination className="extension-tool-pagination" total={filteredTools.length}
            page={currentPage} pageSize={pageSize} change={setToolPage} />}
          {!tools.length && connection?.last_checked_at && !changedConnection && <p className="extension-warning">此服务尚未提供工具。</p>}
          {!!form.allowed_tools.length && !changedConnection && <label className="extension-check extension-warning">
            <input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />
            我授权模型自动调用所选工具，向该服务发送调用参数，并可能读取、发送、修改或删除外部数据。服务可能另行收费。
          </label>}
        </section>
        <label className="extension-check"><input type="checkbox" checked={form.enabled} disabled={!tools.length} onChange={e => setForm({ ...form, enabled: e.target.checked })} />启用</label>
        {(error || paramsQuery.get("discovery") === "failed" && !connection?.last_checked_at) && <p role="alert" className="extension-error">{error || "连接草稿已保存，获取工具失败。请检查服务地址和认证后重新测试。"}</p>}{notice && <p role="status">{notice}</p>}
        {conflict && <div className="admin-inline-confirm"><span>输入已保留，重新加载会放弃修改。</span><button className="danger" type="button" onClick={() => { void resource.reload(); setConflict(false); }}>放弃并重新加载</button></div>}
        <div className="extension-actions"><button type="submit" className="primary" disabled={busy || conflict}>{busy ? <Loader2 size={16} className="spin" /> : <Save size={16} />}保存{tools.length ? "" : "草稿"}</button>{dirty && <span className="muted">未保存</span>}</div>
        {connection && <div className="extension-section">{deleting ? <div className="admin-inline-confirm"><span>删除此连接？已有回答不受影响。</span>
          <button type="button" className="secondary" onClick={() => setDeleting(false)}>保留</button>
          <button type="button" className="danger" disabled={busy} onClick={async () => { setBusy(true); try {
            await request(`/agent/mcp/${connection.id}`, json({ version: connection.version }, "DELETE")); router.push("/extensions?tab=mcp");
          } catch (e) { setError(e instanceof Error ? e.message : "删除失败"); } finally { setBusy(false); } }}>确认删除</button>
        </div> : <button type="button" className="danger" onClick={() => setDeleting(true)}><Trash2 size={16} />删除连接</button>}</div>}
        </fieldset>
      </form>}
    </div>
  </>;
}
