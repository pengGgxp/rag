"use client";

import { useState } from "react";
import { ThinkingConfigFields, thinkingConfigError } from "./thinking-config-fields";
import { emptyThinking, emptyImageConfig, type ChatProtocol, type ImageConfig, type Policy, type ThinkingConfig } from "./model-types";
import { useAdminResource } from "./admin/admin-ui";
import { validCredits } from "./credit-format";
import { ImageConfigFields } from "./image-config-fields";
import "./image-tasks.css";

export type ModelDraft = {
  name: string; model: string; thinking: ThinkingConfig; context_capacity: number | null;
  max_output_tokens: number | null; enabled: boolean; supports_images: boolean; policies: Policy[];
  supports_chat: boolean; chat_protocol: ChatProtocol; image_config: ImageConfig;
};
export const modelCapabilities = ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"];
export function draftModel(model = "", prices: Policy[] = []): ModelDraft {
  return { name: model, model, thinking: structuredClone(emptyThinking), context_capacity: null,
    supports_chat: true, chat_protocol: "chat_completions", image_config: structuredClone(emptyImageConfig),
    max_output_tokens: null, enabled: false, supports_images: false, policies: modelCapabilities.map(capability => ({
      capability, unit: "tokens", unit_size: 1000, credits_per_unit: null, enabled: true, version: 0,
      ...prices.find(p => p.capability === capability),
    })) };
}
export function modelError(value: ModelDraft, enabling = false) {
  if (!value.name.trim() || !value.model.trim()) return "请填写显示名称和模型名";
  if (value.image_config.specs.some(s => s.credits_per_image === null || !validCredits(s.credits_per_image))) return "请为图片规格填写每张积分，免费须明确填写 0";
  const image = value.image_config;
  const keys = image.specs.map(s => `${s.operation}|${s.size}|${s.quality}`);
  if (new Set(keys).size !== keys.length) return "图片规格重复，请移除重复项";
  if (image.specs.some(s => !/^[A-Za-z0-9_:.-]{1,40}$/.test(s.size) || !/^[A-Za-z0-9_.-]{1,40}$/.test(s.quality))) return "请检查图片尺寸及质量或分辨率格式";
  for (const op of ["generate", "edit"] as const) {
    const choice = image.defaults?.[op] ?? { size: image.default_size, quality: image.default_quality };
    if (image[op] && !image.specs.some(s => s.operation === op && s.size === choice.size && s.quality === choice.quality)) return "请为每项启用的图片操作选择已定价的默认规格";
  }
  const thinking = thinkingConfigError(value.thinking);
  if (thinking) return thinking;
  if ((value.context_capacity === null) !== (value.max_output_tokens === null)) return "请同时填写上下文容量和最大输出";
  if (value.context_capacity !== null && value.max_output_tokens !== null && (
    !Number.isInteger(value.context_capacity) || value.context_capacity < 1024 || value.context_capacity > 100000000 ||
    !Number.isInteger(value.max_output_tokens) || value.max_output_tokens < 1 || value.max_output_tokens > 10000000 ||
    value.max_output_tokens >= value.context_capacity * .95)) return "请检查容量与输出限制，输出须留出 5% 上下文余量";
  if (value.policies.some(p => !Number.isInteger(p.unit_size) || p.unit_size < 1 || p.unit_size > 2147483647 ||
    (p.credits_per_unit !== null && !validCredits(p.credits_per_unit))))
    return "单位用量须为正整数，积分须非负且最多四位小数，数值不超过 2147483647";
  if (value.supports_chat && (enabling || value.enabled) && value.policies.some(p => p.credits_per_unit === null ||
    !validCredits(p.credits_per_unit) || !Number.isInteger(p.unit_size) || p.unit_size < 1 || !p.enabled))
    return "启用前请补齐并启用三项价格，免费请明确填写 0";
  return "";
}
export function modelPayload(value: ModelDraft) {
  return { ...value, policies: value.policies.filter(p => p.credits_per_unit !== null).map(
    ({ capability, unit, unit_size, credits_per_unit, enabled }) => ({ capability, unit, unit_size, credits_per_unit, enabled })) };
}

export function ModelConfigFields({ value, change, platform, apiProtocol }: { value: ModelDraft; change: (v: ModelDraft) => void; platform?: string; apiProtocol?: string }) {
  const presets = useAdminResource<{ model: string; platform: string; thinking: Partial<ThinkingConfig>;
    context_capacity: number; max_output_tokens: number; source: string }[]>("/site/admin/model-connections/model-presets");
  const preset = apiProtocol === "responses" ? undefined : presets.data?.find(p => p.model === value.model && p.platform === platform);
  const [confirm, setConfirm] = useState(false);
  return <>
    <section className="model-form-section"><h2>基本信息</h2><div className="provider-form-grid">
      <label>显示名称<input required maxLength={120} value={value.name} onChange={e => change({ ...value, name: e.target.value })} /></label>
      <label>模型名<input required maxLength={200} value={value.model} onChange={e => change({ ...value, model: e.target.value })} /></label>
    </div><div className="provider-form-grid">
      <label className="provider-enabled"><input type="checkbox" checked={value.supports_images}
        onChange={e => change({ ...value, supports_images: e.target.checked })} />支持图片输入</label>
    </div>
    <div className="provider-form-grid">
      <label className="provider-enabled"><input type="checkbox" checked={value.supports_chat} onChange={e => change({ ...value, supports_chat: e.target.checked })} />对话能力</label>
      {value.supports_chat && <label>对话协议<select value={value.chat_protocol} onChange={e => change({ ...value, chat_protocol: e.target.value as ChatProtocol })}>
        <option value="chat_completions">OpenAI Chat Completions</option><option value="responses">OpenAI Responses</option>
        <option value="anthropic">Anthropic Messages</option><option value="gemini">Gemini 原生</option>
      </select></label>}
    </div>
    </section>
    <ImageConfigFields value={value.image_config} change={image_config => change({ ...value, image_config })} />
    {preset && <div className="model-preset"><button className="secondary" type="button" onClick={() => setConfirm(true)}>使用官方能力预设</button>
      {confirm && <div className="admin-inline-confirm" role="alert"><span>将替换思考能力、上下文容量和最大输出设置，价格保持不变。</span>
        <a href={preset.source} target="_blank" rel="noreferrer">查看依据</a>
        <button className="secondary" type="button" onClick={() => setConfirm(false)}>取消</button>
        <button className="primary" type="button" onClick={() => { change({ ...value, thinking: { ...emptyThinking, ...preset.thinking },
          context_capacity: preset.context_capacity, max_output_tokens: preset.max_output_tokens }); setConfirm(false); }}>应用预设</button>
      </div>}
    </div>}
    {value.supports_chat && <details className="model-advanced"><summary>思考能力与上下文</summary>
      <ThinkingConfigFields value={value.thinking} suggestedAdapter={
        value.chat_protocol === "anthropic" || value.chat_protocol === "gemini" ? value.chat_protocol : apiProtocol === "responses" ? "openai" : platform === "deepseek" || platform === "bailian" || platform === "openai" ? platform : undefined}
        onChange={thinking => change({ ...value, thinking })} />
      <section className="model-form-section"><h2>上下文容量</h2>
        <div className="provider-form-grid">
          <label>上下文容量（Token）<input type="number" min={1024} max={100000000} value={value.context_capacity ?? ""}
            onChange={e => change({ ...value, context_capacity: e.target.value ? Number(e.target.value) : null })} /></label>
          <label>最大输出（Token）<input type="number" min={1} max={10000000} value={value.max_output_tokens ?? ""}
            onChange={e => change({ ...value, max_output_tokens: e.target.value ? Number(e.target.value) : null })} /></label>
        </div>
        {value.context_capacity === null && <p className="billing-muted">尚未设置容量，上下文保护暂不可用。</p>}
      </section>
    </details>}
  </>;
}

export { ImageConfigFields } from "./image-config-fields";
