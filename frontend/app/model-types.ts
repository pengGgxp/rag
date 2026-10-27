export type Policy = {
  capability: string;
  unit: string;
  unit_size: number;
  credits_per_unit: number;
  enabled: boolean;
  version: number;
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
};

export const policyLabels: Record<string, string> = {
  llm_input_tokens: "模型输入 Token",
  llm_output_tokens: "模型输出 Token",
  embedding_tokens: "Embedding Token",
  ocr_pages: "OCR 页数",
};
