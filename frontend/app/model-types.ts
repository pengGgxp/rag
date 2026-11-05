export type Policy = {
  capability: string;
  unit: string;
  unit_size: number;
  credits_per_unit: number | null;
  enabled: boolean;
  version: number;
  configured?: boolean;
  template_version?: number | null;
  source?: string;
  effective_credits_per_unit?: string | null;
};

export type ChatProtocol = "chat_completions" | "responses" | "anthropic" | "gemini";
export type ImageConfig = {
  protocol: "openai_images" | "gemini" | "responses";
  image_model?: string | null;
  generate: boolean; edit: boolean; max_count: number; max_references: number;
  default_size: string; default_quality: string;
  specs: { operation: "generate" | "edit"; size: string; quality: string; credits_per_image: number | null; cost_per_image: number | null }[];
};
export const emptyImageConfig: ImageConfig = { protocol: "openai_images", generate: false, edit: false,
  max_count: 1, max_references: 1, default_size: "1024x1024", default_quality: "standard", specs: [] };

export type ModelProvider = {
  id: string;
  key: string;
  name: string;
  model: string;
  supports_images: boolean;
  connection_id: string;
  connection_name: string;
  connection_version: number;
  api_protocol: ChatProtocol;
  chat_protocol: ChatProtocol;
  supports_chat: boolean;
  image_config: ImageConfig;
  platform: string;
  enabled: boolean;
  active: boolean;
  version: number;
  is_default: boolean;
  configured: boolean;
  prices: Policy[];
  workspace_count: number;
  available: boolean;
  unavailable_reason: string | null;
  thinking?: ThinkingConfig;
  thinking_version?: number;
  context_capacity?: number | null;
  max_output_tokens?: number | null;
};

export type ThinkingConfig = {
  kind: "unconfigured" | "unsupported" | "fixed" | "toggle" | "effort" | "budget";
  adapter: "none" | "deepseek" | "bailian" | "openai" | "anthropic" | "gemini";
  options: { id: string; label: string; value: string | number }[];
  default_option_id: string | null;
  budget_min: number;
  budget_max: number;
  supports_tools: boolean;
  stream_only: boolean;
  always_thinking: boolean;
};

export type ThinkingCapability = {
  kind: ThinkingConfig["kind"];
  version: number;
  default_option_id: string | null;
  options: { id: string; label: string; detail: string }[];
};

export const emptyThinking: ThinkingConfig = {
  kind: "unconfigured", adapter: "none", options: [], default_option_id: null,
  budget_min: 1, budget_max: 32768, supports_tools: true, stream_only: false, always_thinking: false,
};

export function availabilityLabel(reason: string | null) {
  return reason === "not_chat_model" ? "图片模型" : reason === "model_disabled" ? "已停用"
    : reason === "connection_disabled" ? "供应商已停用"
    : reason === "pricing_unconfigured" ? "价格待配置"
    : reason === "service_not_ready" ? "连接未就绪"
    : reason ? "暂不可用" : "可用";
}

export const policyLabels: Record<string, string> = {
  storage_package: "存储与下载套餐",
  web_search: "联网搜索",
  web_fetch: "网页读取",
  llm_input_tokens: "模型输入 Token",
  llm_cached_input_tokens: "模型缓存读取 Token",
  llm_output_tokens: "模型输出 Token",
  embedding_tokens: "Embedding Token",
  ocr_pages: "OCR 页数",
};

export function policyPayload(policy: Policy) {
  return { capability: policy.capability, unit: policy.unit, unit_size: policy.unit_size,
    credits_per_unit: policy.credits_per_unit, enabled: policy.enabled };
}
