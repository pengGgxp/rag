"use client";
import { useEffect, useState } from "react";
import { ApiError, request } from "../api";
import { useAuth } from "../auth";
import type { MCPConnection, PersonalSkill, WorkbenchStatus } from "./extension-data";

export function useExtensionCatalog(open: boolean) {
  const { session } = useAuth();
  const [skills, setSkills] = useState<PersonalSkill[]>([]);
  const [platform, setPlatform] = useState<PersonalSkill[]>([]);
  const [servers, setServers] = useState<MCPConnection[]>([]);
  const [workbench, setWorkbench] = useState<WorkbenchStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError(""); setSkills([]); setPlatform([]); setServers([]);
    Promise.all([request<PersonalSkill[]>("/agent/skills", { signal: controller.signal }),
      request<PersonalSkill[]>("/agent/skills/platform", { signal: controller.signal }),
      request<MCPConnection[]>("/agent/mcp", { signal: controller.signal }),
      request<WorkbenchStatus>("/question-workbench", { signal: controller.signal })])
      .then(([s, p, m, w]) => { if (!controller.signal.aborted) { setSkills(s); setPlatform(p); setServers(m); setWorkbench(w); } })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof ApiError ? e.message : "扩展加载失败，请重试"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, attempt, session.workspace_id]);
  return { skills, platform, servers, workbench, loading, error, retry: () => setAttempt(value => value + 1) };
}

export async function grantWorkbench(status: WorkbenchStatus | null): Promise<boolean> {
  if (!status?.available) throw new Error("题目工作台暂不可用");
  if (status.enabled) return true;
  if (!window.confirm("允许模型读取、新建和修改当前工作区中属于你的题目与试卷，并生成可下载文件？只有本会话选择后才会提供这些工具；你可以随时撤销授权。")) return false;
  await request("/question-workbench/grant", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true, version: status.version, acknowledge: true }) });
  return true;
}
