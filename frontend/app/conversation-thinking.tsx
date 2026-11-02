"use client";

import { useEffect, useId, useState } from "react";
import { Brain, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { request } from "./api";
import type { DisplayBlock } from "./conversation-display";

export function ConversationThinking({ block, conversationId, messageId, messageStatus }: {
  block: Extract<DisplayBlock, { type: "thinking" }>;
  conversationId: string; messageId: string; messageStatus: string;
}) {
  const running = messageStatus === "streaming" && block.status === "running";
  const [choice, setChoice] = useState<boolean | null>(null);
  const expanded = choice ?? running;
  const [savedText, setSavedText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const text = block.text ?? savedText;
  const regionId = useId();
  useEffect(() => {
    if (!expanded || text !== null && text !== undefined) return;
    let active = true;
    const controller = new AbortController();
    setError("");
    void request<{ blocks: { id: string; text: string }[] }>(
      `/conversations/${conversationId}/messages/${messageId}/thinking`, { signal: controller.signal },
    ).then(value => {
      if (!active) return;
      setSavedText(value.blocks.find(item => item.id === block.id)?.text ?? "");
    }).catch(() => { if (active) setError("暂时无法读取，请重试"); });
    return () => { active = false; controller.abort(); };
  }, [expanded, text, conversationId, messageId, block.id, attempt]);
  const label = running ? "正在思考" : block.status === "completed" ? "思考过程" : "思考已中断";
  return <div className="conversation-thinking">
    <button type="button" className="activity-toggle thinking-toggle" aria-expanded={expanded}
      aria-controls={regionId} aria-label={`${expanded ? "收起" : "展开"}第 ${block.round} 轮思考`}
      onClick={() => setChoice(!expanded)}>
      <ChevronRight size={14} className={`activity-chevron${expanded ? " expanded" : ""}`} aria-hidden="true" />
      {running ? <Loader2 size={14} className="spin activity-current-icon" aria-hidden="true" />
        : <Brain size={14} aria-hidden="true" />}
      <span>{label}</span>
    </button>
    {expanded && <div className="thinking-content" id={regionId}>
      {error ? <div className="thinking-read-error" role="alert"><span>{error}</span>
        <button type="button" className="text-button" onClick={() => setAttempt(value => value + 1)}>
          <RefreshCw size={13} />重试</button></div>
        : text === null || text === undefined ? <span role="status">正在读取…</span>
        : <p>{text || "没有可查看的思考内容"}</p>}
    </div>}
  </div>;
}
