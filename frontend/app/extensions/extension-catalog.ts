"use client";
import { useEffect, useState } from "react";
import { ApiError, request } from "../api";
import { useAuth } from "../auth";
import type { MCPConnection, PersonalSkill } from "./extension-data";

export function useExtensionCatalog(open: boolean) {
  const { session } = useAuth();
  const [skills, setSkills] = useState<PersonalSkill[]>([]);
  const [platform, setPlatform] = useState<PersonalSkill[]>([]);
  const [servers, setServers] = useState<MCPConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError(""); setSkills([]); setPlatform([]); setServers([]);
    Promise.all([request<PersonalSkill[]>("/agent/skills", { signal: controller.signal }),
      request<PersonalSkill[]>("/agent/skills/platform", { signal: controller.signal }),
      request<MCPConnection[]>("/agent/mcp", { signal: controller.signal })])
      .then(([s, p, m]) => { if (!controller.signal.aborted) { setSkills(s); setPlatform(p); setServers(m); } })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof ApiError ? e.message : "扩展加载失败，请重试"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, attempt, session.workspace_id]);
  return { skills, platform, servers, loading, error, retry: () => setAttempt(value => value + 1) };
}
