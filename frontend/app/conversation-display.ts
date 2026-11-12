import type { ConversationActivityItem, KnowledgeBaseResult, SavedDocument } from "./conversation-activity";
import type { ImageTaskResult } from "./image-task-result";
import type { Preview } from "./cloud-preview";

export type DisplayBlock = { id: string; type: "text"; text: string }
  | { id: string; type: "activity"; call_id: string }
  | { id: string; type: "thinking"; round: number; status: string; text?: string };
export type ToolEvent = {
  call_id: string; name: string; label: string; kind: string; status: string;
  summary?: string; result_count?: number;
  files?: { id: string; path: string; size_bytes: number; version: number }[];
  saved_document?: SavedDocument;
  image_task?: ImageTaskResult;
  preview?: Preview;
  knowledge_base?: KnowledgeBaseResult;
};

export function activityItem(item: ToolEvent): ConversationActivityItem {
  return {
    id: item.call_id, label: item.label, summary: item.summary,
    files: item.files,
    saved_document: item.saved_document,
    image_task: item.image_task,
    preview: item.preview,
    knowledge_base: item.knowledge_base,
    kind: item.kind === "knowledge" ? "retrieval" : item.kind === "compaction" ? "compaction" : "tool",
    status: item.status === "error" || item.status === "failed" ? "failed"
      : item.status === "interrupted" ? "uncertain"
      : ["waiting", "running", "completed", "cancelled", "uncertain"].includes(item.status)
        ? item.status as ConversationActivityItem["status"] : "waiting",
  };
}

export function appendText(blocks: DisplayBlock[], blockId: string, text: string) {
  const exists = blocks.some(block => block.id === blockId);
  return exists ? blocks.map(block => block.id === blockId && block.type === "text"
    ? { ...block, text: block.text + text } : block)
    : [...blocks, { id: blockId, type: "text" as const, text }];
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
