"use client";

import { useEffect, useState } from "react";
import { Loader2, Monitor, RotateCcw, Square } from "lucide-react";
import Link from "next/link";
import { ApiError, request } from "./api";
import { json } from "./admin/admin-ui";
import { Tooltip } from "../components/ui/tooltip";
import { formatCredits } from "./credit-format";
import "./cloud-computer.css";

type Computer = { id: string; status: string; summary: string; terminal: boolean; credits: string;
  credits_per_minute: string; idle_ends_at: string | null };
type Command = { id: string; status: string; terminal: boolean; cancel_requested: boolean;
  result: { error?: string; generated_files?: { id: string; path: string }[] } };
type Catalog = { available: boolean; credits_per_minute?: number; idle_seconds?: number };

export function CloudComputerPanel({ conversationId, workspaceId }: { conversationId: string | null; workspaceId: string }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [computer, setComputer] = useState<Computer | null>(null);
  const [commands, setCommands] = useState<Command[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let disposed = false;
    setCatalog(null); setComputer(null); setCommands([]); setError("");
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      if (document.hidden) { timer = setTimeout(refresh, 5000); return; }
      try {
        const value = await request<Catalog>("/cloud-computer/catalog");
        if (disposed) return;
        setCatalog(value);
        if (conversationId) {
          const current = await request<{ computer: Computer | null }>(`/conversations/${conversationId}/cloud-computer`);
          if (disposed) return;
          setComputer(current.computer);
          if (current.computer) {
            const history = await request<{ items: Command[] }>(`/conversations/${conversationId}/cloud-computer/commands`);
            if (disposed) return;
            setCommands(history.items);
          }
        }
        setError("");
      } catch (cause) {
        if (disposed) return;
        if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) return;
        setError("云电脑状态更新失败");
      }
      if (!disposed) timer = setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
  }, [conversationId, workspaceId, revision]);
  async function act(path: string) {
    setBusy(true); setError("");
    try { await request(path, json({})); setRevision(value => value + 1); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败，请重试"); }
    finally { setBusy(false); }
  }
  if (!catalog?.available && !computer && !error) return null;
  const last = commands[0];
  const price = catalog?.available ? `云电脑 ${formatCredits(Number(catalog.credits_per_minute))} 积分/分钟 · 按秒计费，闲置 ${Math.ceil((catalog.idle_seconds || 600) / 60)} 分钟结束` : "云电脑";
  return <div className="cloud-computer-panel">
    <div className="cloud-computer-line">
      <Monitor size={14} aria-hidden="true" />
      {computer ? <>
        <span role="status">{!computer.terminal && <Loader2 size={13} className="spin" />}{computer.summary}</span>
        <span>{computer.terminal ? "消费" : "累计"} {formatCredits(Number(computer.credits))} 积分</span>
        {!computer.terminal && <>
          {computer.idle_ends_at && <span>{new Date(computer.idle_ends_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 闲置结束</span>}
          <Tooltip content="结束云电脑"><button type="button" className="icon-button" aria-label="结束云电脑" disabled={busy || computer.status === "stopping"} onClick={() => void act(`/conversations/${conversationId}/cloud-computer/end`)}><Square size={14} /></button></Tooltip>
        </>}
      </> : <span>{price}</span>}
      {computer && !computer.terminal && <Tooltip content={`按存续时间收费，包含等待；当前单价 ${computer.credits_per_minute} 积分/分钟。结束后文件保存在资源库。`}><button type="button" className="text-button cloud-computer-price">{computer.credits_per_minute} 积分/分钟</button></Tooltip>}
    </div>
    {computer?.terminal && catalog?.available && <div className="cloud-computer-line"><span>{price}</span></div>}
    {last && !last.terminal && <div className="cloud-computer-line"><span>正在处理文件</span><Tooltip content="停止当前命令"><button type="button" className="icon-button" aria-label="停止当前命令" disabled={busy || last.cancel_requested} onClick={() => void act(`/cloud-computer/commands/${last.id}/cancel`)}><Square size={14} /></button></Tooltip></div>}
    {commands.some(command => command.status === "completed") && <Link href="/resources">打开资源库</Link>}
    {error && <div className="cloud-computer-line" role="alert"><span>{error}</span><button type="button" className="icon-button" aria-label="刷新云电脑状态" onClick={() => setRevision(value => value + 1)}><RotateCcw size={14} /></button></div>}
  </div>;
}
