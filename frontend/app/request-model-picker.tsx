"use client";

import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { BrainCircuit, Check, ChevronDown, Layers3, Search } from "lucide-react";
import { Dialog } from "../components/ui/dialog";
import { Tooltip } from "../components/ui/tooltip";
import { availabilityLabel, type ThinkingCapability } from "./model-types";

export type RequestModelChoice = {
  id: string;
  name: string;
  model: string;
  available: boolean;
  supports_images?: boolean;
  unavailable_reason?: string | null;
  thinking?: ThinkingCapability;
};

export function RequestModelPicker({ models, modelId, thinkingId, disabled, loading, onModelChange, onThinkingChange }: {
  models: RequestModelChoice[];
  modelId: string;
  thinkingId: string;
  disabled: boolean;
  loading: boolean;
  onModelChange: (id: string) => void;
  onThinkingChange: (id: string) => void;
}) {
  const [view, setView] = useState<"models" | "thinking" | null>(null);
  const [search, setSearch] = useState("");
  const modelRef = useRef<HTMLButtonElement>(null);
  const thinkingRef = useRef<HTMLButtonElement>(null);
  const model = models.find(item => item.id === modelId);
  const capability = model?.thinking;
  const option = capability?.options.find(item => item.id === thinkingId);
  useEffect(() => { if (disabled) setView(null); }, [disabled]);
  const filtered = models.filter(item => `${item.name} ${item.model}`.toLowerCase().includes(search.trim().toLowerCase()));
  const tabModelId = filtered.some(item => item.id === modelId && item.available) ? modelId : filtered.find(item => item.available)?.id;
  function open(next: "models" | "thinking") {
    setSearch("");
    setView(current => current === next ? null : next);
  }
  function moveFocus(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)'));
    if (!options.length) return;
    event.preventDefault();
    const current = options.findIndex(item => item === document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
    options[next].focus();
  }
  return <div className="composer-model-controls">
    <Tooltip content={model ? `${model.name} · ${model.model}` : "选择本次请求模型"}>
      <button ref={modelRef} type="button" className="request-picker-trigger request-model-trigger"
        aria-label="本次请求模型" aria-haspopup="dialog" aria-expanded={view === "models"} data-value={modelId}
        disabled={disabled} onClick={() => open("models")}>
        <Layers3 size={15} aria-hidden="true" /><span>{loading ? "加载模型…" : model?.name ?? "选择模型"}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>
    </Tooltip>
    {capability?.options.length ? <Tooltip content={option ? `${option.label} · ${option.detail}` : "选择本次请求思考设置"}>
      <button ref={thinkingRef} type="button" className="request-picker-trigger request-thinking-trigger"
        aria-label="本次请求思考设置" aria-haspopup="dialog" aria-expanded={view === "thinking"} data-value={thinkingId}
        disabled={disabled} onClick={() => open("thinking")}>
        <BrainCircuit size={15} aria-hidden="true" /><span>{option?.label ?? "思考设置"}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>
    </Tooltip> : capability?.kind === "fixed" ? <span className="thinking-fixed"><BrainCircuit size={15} aria-hidden="true" />固定思考</span> : null}
    {view && <Dialog key={view} title={view === "models" ? "选择模型" : "思考设置"}
      anchor={view === "models" ? modelRef.current : thinkingRef.current} onClose={() => setView(null)}>
      <div className="request-choice-heading"><span>{view === "models" ? "模型" : "思考设置"}</span>
        <small>{view === "models" ? "本次请求" : model?.name}</small></div>
      {view === "models" && models.length > 5 && <label className="request-choice-search"><Search size={15} aria-hidden="true" />
        <input aria-label="搜索模型" placeholder="搜索模型…" value={search} onChange={event => setSearch(event.target.value)}
          onKeyDown={event => {
            if (event.key !== "ArrowDown") return;
            event.preventDefault();
            event.currentTarget.closest('[role="dialog"]')?.querySelector<HTMLElement>('[role="option"]:not(:disabled)')?.focus();
          }} /></label>}
      <div className="request-choice-list" role="listbox" aria-label={view === "models" ? "可用模型" : "思考选项"} onKeyDown={moveFocus}>
        {view === "models" ? filtered.map(item => <button type="button" role="option" key={item.id}
          data-value={item.id} aria-selected={item.id === modelId} disabled={!item.available}
          tabIndex={item.id === tabModelId ? 0 : -1}
          onClick={() => { onModelChange(item.id); setView(null); }}>
          <span className="request-choice-copy"><strong>{item.name}</strong><small>{item.model}{item.supports_images ? " · 支持图片" : ""}</small>
            {!item.available && <span className="request-choice-unavailable">{availabilityLabel(item.unavailable_reason ?? null)}</span>}</span>
          {item.id === modelId && <Check size={16} aria-hidden="true" />}
        </button>) : capability?.options.map(item => <button type="button" role="option" key={item.id}
          data-value={item.id} aria-selected={item.id === thinkingId} tabIndex={item.id === thinkingId ? 0 : -1}
          onClick={() => { onThinkingChange(item.id); setView(null); }}>
          <span className="request-choice-copy"><strong>{item.label}</strong><small>{item.detail}</small></span>
          {item.id === thinkingId && <Check size={16} aria-hidden="true" />}
        </button>)}
        {view === "models" && !filtered.length && <p className="request-choice-empty">{search ? "没有匹配的模型" : "暂无可用模型"}</p>}
      </div>
    </Dialog>}
  </div>;
}
