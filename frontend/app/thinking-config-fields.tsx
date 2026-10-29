"use client";

import { ChevronDown, Plus, SlidersHorizontal, Trash2 } from "lucide-react";
import { Tooltip } from "../components/ui/tooltip";
import { emptyThinking, type ThinkingConfig } from "./model-types";

const levels = {
  deepseek: ["disabled", "low", "high", "max"],
  openai: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  bailian: ["low", "medium", "high"],
};
const labels: Record<string, string> = {
  disabled: "关闭", none: "无", minimal: "最少", low: "低", medium: "中", high: "高", xhigh: "更高", max: "最高",
};

export function thinkingConfigError(value: ThinkingConfig): string {
  if (value.always_thinking && value.options.some(item => item.value === "disabled"))
    return "始终思考的模型不能配置关闭选项。";
  if (["unconfigured", "unsupported", "fixed"].includes(value.kind)) return "";
  if (!value.options.length) return value.kind === "budget" ? "至少配置一个预算预设。" : "至少选择一个支持的等级。";
  if (!value.options.some(item => item.id === value.default_option_id)) return "请选择默认思考设置。";
  if (value.options.some(item => !item.label.trim())) return "选项显示名称不能为空。";
  if (new Set(value.options.map(item => item.value)).size !== value.options.length) return "不同预设不能使用相同参数值。";
  if (value.kind === "budget" && (!Number.isInteger(value.budget_min) || !Number.isInteger(value.budget_max)
    || value.budget_min < 1 || value.budget_max > 2147483647 || value.budget_min > value.budget_max
    || value.options.some(item => !Number.isInteger(item.value) || Number(item.value) < value.budget_min || Number(item.value) > value.budget_max)))
    return "预算必须为模型支持范围内的整数。";
  return "";
}

export function ThinkingConfigFields({ value, onChange, suggestedAdapter }: {
  value: ThinkingConfig; onChange: (next: ThinkingConfig) => void; suggestedAdapter?: ThinkingConfig["adapter"];
}) {
  const configurable = !["unconfigured", "unsupported"].includes(value.kind);
  function reset(kind: ThinkingConfig["kind"], adapter: ThinkingConfig["adapter"]) {
    if (kind === "unconfigured" || kind === "unsupported") {
      onChange({ ...emptyThinking, kind }); return;
    }
    const next = { ...emptyThinking, kind, adapter };
    if (kind === "toggle") {
      next.options = [{ id: "on", label: "开启", value: "enabled" }, { id: "off", label: "关闭", value: "disabled" }];
      next.default_option_id = "on";
    } else if (kind === "effort") {
      next.options = [{ id: "high", label: "高", value: "high" }];
      next.default_option_id = "high";
    } else if (kind === "budget") {
      next.options = [{ id: "budget-1", label: "标准", value: 2048 }];
      next.default_option_id = "budget-1";
    }
    onChange(next);
  }
  function patchOption(id: string, patch: Partial<ThinkingConfig["options"][number]>) {
    onChange({ ...value, options: value.options.map(item => item.id === id ? { ...item, ...patch } : item) });
  }
  function removeOption(id: string) {
    const options = value.options.filter(item => item.id !== id);
    onChange({ ...value, options, default_option_id: value.default_option_id === id ? options[0]?.id ?? null : value.default_option_id });
  }
  return <section className="model-form-section thinking-config">
    <h2>思考能力</h2>
    <div className="provider-form-grid">
      <label>思考方式<select value={value.kind} onChange={event => {
        const kind = event.target.value as ThinkingConfig["kind"];
        const suggested = suggestedAdapter && suggestedAdapter !== "none" ? suggestedAdapter : "deepseek";
        const adapter = kind === "budget" ? "bailian" : value.adapter === "none"
          ? kind === "toggle" && suggested === "openai" ? "deepseek" : suggested
          : value.adapter === "openai" && kind === "toggle" ? "deepseek" : value.adapter;
        reset(kind, adapter);
      }}>
        <option value="unconfigured">未配置 · 上游默认</option><option value="unsupported">不支持思考</option>
        <option value="fixed">固定思考</option><option value="toggle">开启 / 关闭</option>
        <option value="effort">思考等级</option><option value="budget">Token 预算</option>
      </select></label>
      {configurable && <label>接口适配器<select value={value.adapter} onChange={event => reset(value.kind, event.target.value as ThinkingConfig["adapter"])}>
        {value.kind !== "budget" && <option value="deepseek">DeepSeek</option>}
        <option value="bailian">百炼兼容接口</option>
        {(value.kind === "effort" || value.kind === "fixed") && <option value="openai">OpenAI Chat Completions</option>}
      </select></label>}
    </div>
    {configurable && <>
      <details className="thinking-advanced">
      <summary><SlidersHorizontal size={15} aria-hidden="true" />高级兼容设置<ChevronDown size={15} aria-hidden="true" /></summary>
      <div className="thinking-capability-flags">
        <label className="provider-enabled"><input type="checkbox" checked={value.supports_tools}
          onChange={event => onChange({ ...value, supports_tools: event.target.checked })} />支持 Chat Completions 工具调用</label>
        {value.adapter === "bailian" && <>
          <label className="provider-enabled"><input type="checkbox" checked={value.stream_only}
            onChange={event => onChange({ ...value, stream_only: event.target.checked })} />仅支持流式响应</label>
          {(value.kind === "budget" || value.kind === "effort") && <label className="provider-enabled"><input type="checkbox" checked={value.always_thinking}
            onChange={event => onChange({ ...value, always_thinking: event.target.checked })} />模型始终思考</label>}
        </>}
      </div>
      </details>
      {value.kind === "effort" && <fieldset className="thinking-levels"><legend>模型支持的等级</legend>
        <div className="thinking-option-heading" aria-hidden="true"><span>可选参数</span><span>显示名称</span></div>
        {levels[value.adapter as keyof typeof levels]?.map(level => {
          const chosen = value.options.find(item => item.value === level);
          return <div className="thinking-option-row" data-selected={!!chosen} key={level}>
            <label className="provider-enabled"><input type="checkbox" checked={!!chosen} onChange={event => {
              if (!event.target.checked) { removeOption(chosen!.id); return; }
              onChange({ ...value, options: [...value.options, { id: level, label: labels[level], value: level }],
                default_option_id: value.default_option_id ?? level });
            }} />{labels[level]} <code>{level}</code></label>
            {chosen ? <label className="thinking-option-name"><span>显示名称</span><input aria-label={`${level} 显示名称`} required maxLength={40}
              value={chosen.label} onChange={event => patchOption(chosen.id, { label: event.target.value })} /></label>
              : <span className="thinking-option-empty" aria-hidden="true">未启用</span>}
          </div>;
        })}
      </fieldset>}
      {value.kind === "budget" && <>
        <div className="provider-form-grid thinking-budget-range">
          <label>模型最小预算（Token）<input type="number" required min={1} max={2147483647} step={1} value={value.budget_min}
            onChange={event => onChange({ ...value, budget_min: Number(event.target.value) })} /></label>
          <label>模型最大预算（Token）<input type="number" required min={value.budget_min} max={2147483647} step={1} value={value.budget_max}
            onChange={event => onChange({ ...value, budget_max: Number(event.target.value) })} /></label>
        </div>
        <div className="thinking-presets">
          {value.options.map(option => <div className="thinking-budget-row" key={option.id}>
            <label>预设名称<input required maxLength={40} value={option.label} onChange={event => patchOption(option.id, { label: event.target.value })} /></label>
            <label>预算（Token）<span className="policy-input"><input required type="number" step={1} min={value.budget_min} max={value.budget_max} value={option.value}
              onChange={event => patchOption(option.id, { value: Number(event.target.value) })} /><span aria-hidden="true">Token</span></span></label>
            <Tooltip content={`删除${option.label}预设`}><button className="icon-button" type="button" aria-label={`删除${option.label}预设`}
              onClick={() => removeOption(option.id)}><Trash2 size={16} /></button></Tooltip>
          </div>)}
          <button type="button" className="secondary" disabled={value.options.length >= 10} onClick={() => {
            let index = 1;
            while (value.options.some(item => item.id === `budget-${index}`)) index++;
            let tokens = value.budget_min;
            while (value.options.some(item => item.value === tokens) && tokens < value.budget_max) tokens++;
            onChange({ ...value, options: [...value.options, { id: `budget-${index}`, label: `预设 ${index}`, value: tokens }],
              default_option_id: value.default_option_id ?? `budget-${index}` });
          }}><Plus size={15} />添加预算预设</button>
        </div>
      </>}
      {value.options.length > 0 && <div className="provider-form-grid">
        <label>默认思考设置<select value={value.default_option_id ?? ""} required onChange={event => onChange({ ...value, default_option_id: event.target.value })}>
          {value.options.map(option => <option key={option.id} value={option.id}>{option.label} · {option.value}{value.kind === "budget" ? " Token" : ""}</option>)}
        </select></label>
      </div>}
    </>}
    {thinkingConfigError(value) && <p className="form-error" role="alert">{thinkingConfigError(value)}</p>}
  </section>;
}
