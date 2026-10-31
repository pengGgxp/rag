export type KnowledgeBase = {
  id: string;
  name: string;
  description?: string | null;
  tags: string[];
  status: string;
  document_count: number;
};

export type SourceDocument = {
  id: string;
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
  job?: { id: string; state: string; error_detail?: string | null };
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

export type WorkspaceScreen = "catalog" | "knowledge" | "chat" | "extensions" | "files";
export type KnowledgeView =
  | "documents"
  | "retrieval"
  | "permissions"
  | "settings";
