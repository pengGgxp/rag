export type Policy = {
  capability: string;
  unit: string;
  unit_size: number;
  credits_per_unit: number | null;
  enabled: boolean;
  version: number;
  configured?: boolean;
  template_version?: number | null;
};

export type ModelProvider = {
  id: string;
  key: string;
  name: string;
  base_url: string;
  model: string;
  timeout_seconds: number;
  enabled: boolean;
  active: boolean;
  has_api_key: boolean;
  published: boolean;
  version: number;
  is_default: boolean;
  configured: boolean;
  prices: Policy[];
  workspace_count: number;
};

export const policyLabels: Record<string, string> = {
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
