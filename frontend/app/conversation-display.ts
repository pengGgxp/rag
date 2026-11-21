import type { ConversationActivityItem, KnowledgeBaseResult, SavedDocument } from "./conversation-activity";
import type { ImageTaskResult } from "./image-task-result";
import type { Preview } from "./cloud-preview";
import type { WorkspaceFileReference } from "./workspace-file-link";
import type { ChatAttachment } from "./chat-attachments";
import type { MentionDocument, MentionKnowledgeBase, MentionResource } from "./document-mention-menu";

export type DisplayBlock = { id: string; type: "text"; text: string; stage?: "pending" | "commentary" | "intermediate" | "final"; status?: string }
  | { id: string; type: "user"; text: string; attachments?: ChatAttachment[];
    document_mentions?: MentionDocument[]; knowledge_base_mentions?: MentionKnowledgeBase[]; resource_mentions?: MentionResource[] }
  | { id: string; type: "activity"; call_id: string }
  | { id: string; type: "thinking"; round: number; status: string; text?: string };
export type ToolEvent = {
  call_id: string; execution_id?: string; name: string; label: string; kind: string; status: string;
  outcome?: "success" | "feedback" | "blocked" | "failed" | "uncertain";
  summary?: string; result_count?: number;
  files?: WorkspaceFileReference[];
  deliverables?: WorkspaceFileReference[];
  saved_document?: SavedDocument;
  image_task?: ImageTaskResult;
  preview?: Preview;
  knowledge_base?: KnowledgeBaseResult;
};

type Narrative = Extract<DisplayBlock, { type: "text" | "user" }>;
export type ProcessBlock = { id: string; type: "process"; blocks: Exclude<DisplayBlock, { type: "user" }>[] };

export function processBlocks(blocks: DisplayBlock[]): (Narrative | ProcessBlock)[] {
  const result: (Narrative | ProcessBlock)[] = [];
  for (const block of blocks) {
    if (block.type === "user" || (block.type === "text" && block.stage !== "commentary")) {
      if (block.text) result.push(block);
      continue;
    }
    const previous = result.at(-1);
    if (previous?.type === "process") previous.blocks.push(block);
    else result.push({ id: `process-${block.id}`, type: "process", blocks: [block] });
  }
  return result;
}

export function activityItem(item: ToolEvent): ConversationActivityItem {
  return {
    id: item.call_id, label: item.label, summary: item.summary,
    executionId: item.execution_id,
    files: item.files,
    deliverables: item.deliverables ?? (["read_file", "read_image", "get_workspace_file_link", "list_files"].includes(item.name)
      ? [] : item.files),
    saved_document: item.saved_document,
    image_task: item.image_task,
    preview: item.preview,
    knowledge_base: item.knowledge_base,
    kind: item.kind === "knowledge" ? "retrieval" : item.kind === "compaction" ? "compaction" : "tool",
    status: item.status === "uncertain" || item.outcome === "uncertain" ? "uncertain"
      : (item.status === "error" || item.status === "failed") && item.outcome === "feedback" ? "feedback"
      : (item.status === "error" || item.status === "failed") && item.outcome === "blocked" ? "blocked"
      : item.status === "error" || item.status === "failed" ? "failed"
      : item.status === "interrupted" ? "uncertain"
      : ["waiting", "running", "completed", "cancelled", "uncertain"].includes(item.status)
        ? item.status as ConversationActivityItem["status"] : "waiting",
  };
}

export function processHeadline(items: ConversationActivityItem[], finished: boolean, thinking = false) {
  const current = items.find(item => item.status === "running" || item.status === "waiting");
  if (current) return `${current.label} · ${current.status === "waiting" ? "等待执行" : "进行中"}`;
  if (!finished) return thinking ? "正在思考" : items.at(-1)?.status === "feedback" ? "正在调整操作" : "正在继续处理";
  return items.length ? `处理记录 · ${items.length} 项操作` : "思考记录";
}

export function activityFiles(items: ConversationActivityItem[]) {
  const key = (file: WorkspaceFileReference) => file.id;
  const deliverables = new Set(items.flatMap(item => [...(item.deliverables ?? item.files ?? []),
    ...(item.image_task?.files ?? [])].map(key)));
  const files = new Map<string, WorkspaceFileReference>();
  for (const item of items) for (const file of [...(item.files ?? []), ...(item.deliverables ?? []), ...(item.image_task?.files ?? [])]) {
    const id = key(file);
    if (deliverables.has(id)) files.set(id, { ...files.get(id), ...file });
  }
  return [...files.values()];
}

export function appendText(blocks: DisplayBlock[], blockId: string, text: string) {
  const exists = blocks.some(block => block.id === blockId);
  return exists ? blocks.map(block => block.id === blockId && block.type === "text"
    ? { ...block, text: block.text + text } : block)
    : [...blocks, { id: blockId, type: "text" as const, text, stage: "pending" as const, status: "running" }];
}

export function appendActivity(blocks: DisplayBlock[], callId: string) {
  return blocks.some(block => block.type === "activity" && block.call_id === callId) ? blocks
    : [...blocks, { id: `activity-${callId}`, type: "activity" as const, call_id: callId }];
}

export function updateThinking(blocks: DisplayBlock[], update: {
  block_id: string; round: number; text?: string; status?: string;
}): DisplayBlock[] {
  const exists = blocks.some(block => block.id === update.block_id);
  if (!exists && !update.text) return blocks;
  return exists ? blocks.map(block => block.id === update.block_id && block.type === "thinking"
    ? { ...block, text: (block.text ?? "") + (update.text ?? ""), status: update.status ?? block.status } : block)
    : [...blocks, { id: update.block_id, type: "thinking", round: update.round,
      status: update.status ?? "running", text: update.text }];
}
