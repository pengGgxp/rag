export type KnowledgeBase = {
  id: string;
  name: string;
  description?: string | null;
  tags: string[];
  status: string;
  document_count: number;
};

export type IngestionRetryProgress = { number: number; maximum: number };
export type DocumentFailure = {
  code: string;
  stage: string;
  message: string;
  retryable: boolean;
  request_id: string;
};

export function ingestionRetryLabel(progress?: IngestionRetryProgress | null) {
  return progress ? `正在自动重试 ${progress.number}/${progress.maximum}` : undefined;
}

export function documentRetryLabel(
  status: string | undefined,
  state: string | undefined,
  retryAfter?: string | null,
  progress?: IngestionRetryProgress | null,
) {
  if (status !== "processing") return;
  if (state === "queued" && retryAfter) return "解析中 · 正在自动重试";
  if (state === "running") return ingestionRetryLabel(progress);
}

export function documentIssue(doc: SourceDocument) {
  return documentRetryLabel(doc.status, doc.job?.state, doc.job?.retry_after, doc.job?.retry_progress)
    ? undefined : doc.failure?.message || doc.job?.failure?.message;
}

export type SourceDocument = {
  id: string;
  knowledge_base_id: string;
  content_origin?: string;
  filename: string;
  version_group_id: string;
  version_number: number;
  is_active: boolean;
  supersedes_id?: string | null;
  status: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  parser_name?: string | null;
  error_detail?: string | null;
  failure?: DocumentFailure | null;
  job?: {
    id: string;
    state: string;
    error_detail?: string | null;
    retry_progress?: IngestionRetryProgress | null;
    retry_after?: string | null;
    failure?: DocumentFailure | null;
    phase?: string | null;
  };
};

export type Health = {
  status: string;
  embedding: { configured: boolean; model: string; dimension: number };
  llm: { configured: boolean; model: string };
};

export type Preview = {
  id?: string;
  filename: string;
  text: string;
  detail: string;
  error?: string | null;
};

export type WorkspaceScreen = "catalog" | "knowledge" | "chat" | "extensions" | "files" | "billing";
export type KnowledgeView = "documents" | "retrieval" | "settings";
