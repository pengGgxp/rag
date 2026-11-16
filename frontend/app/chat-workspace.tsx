"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Fragment, FormEvent, useCallback, useEffect, useLayoutEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  ArrowDown,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Copy,
  FileText,
  Globe,
  Loader2,
  Minimize2,
  Plus,
  Puzzle,
  RefreshCw,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { ApiError, request, streamRequest } from "./api";
import { ConversationHistory, InlineConversationTitle, type HistoryItem } from "./conversation-history";
import { useAuth } from "./auth";
import type { ThinkingCapability } from "./model-types";
import { Modal } from "./modal";
import { WorkspaceNavigation } from "./workspace-shell";
import { WorkspaceHeader, WorkspaceSkeleton } from "./workspace-header";
import { useUiStore } from "./ui-store";
import { AnswerFeedback, type Feedback } from "./answer-feedback";
import { TraceInspector } from "./retrieval-panel";
import { Motion } from "../components/ui/motion";
import "./chat.css";
import { useRouter, useSearchParams } from "next/navigation";
import { RequestModelPicker } from "./request-model-picker";
import { ExtensionPicker } from "./extensions/extension-picker";
import { ExtensionSlashMenu, type SlashMenuHandle } from "./extensions/extension-slash-menu";
import type { ExtensionSelection } from "./extensions/extension-data";
import { DocumentMentionMenu, type MentionDocument, type MentionKnowledgeBase, type MentionResource, type MentionMenuHandle, type MentionReference } from "./document-mention-menu";
import { ConversationActivity } from "./conversation-activity";
import { CloudComputerPanel } from "./cloud-computer";
import { CloudComputerProvider } from "./cloud-computer-context";
import { ConversationThinking } from "./conversation-thinking";
import { citationMarkers } from "./citation-markers";
import { Tooltip } from "../components/ui/tooltip";
import { ConversationContextControl, type ConversationContextState } from "./conversation-context-control";
import { acceptContextState } from "./conversation-context-state";
import { activityItem, appendActivity, appendText, processBlocks, updateThinking, type DisplayBlock, type ToolEvent } from "./conversation-display";
import "./conversation-experience.css";
import { AttachmentButton, DraftAttachments, MessageAttachments, useChatAttachments, type ChatAttachment } from "./chat-attachments";

type Library = {
  id: string;
  name: string;
  description?: string | null;
  document_count: number;
};
type Citation = {
  content_origin?: string;
  chunk_id: string;
  filename: string;
  text: string;
  char_start: number;
  char_end: number;
  heading_path?: string | null;
  page_number?: number | null;
  version_number?: number | null;
  knowledge_base_name: string;
  knowledge_base_id: string;
};
type Trace = {
  id: string;
  knowledge_base_name: string;
  knowledge_base_id: string;
};
type WebSource = { source_id: string; type: "web"; title: string; url: string; fetched_at: string; site_name?: string | null; call_ids?: string[] };
type Message = {
  id: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  attachments?: ChatAttachment[];
  document_mentions?: MentionDocument[];
  knowledge_base_mentions?: MentionKnowledgeBase[];
  resource_mentions?: MentionResource[];
  status: string;
  request_id?: string | null;
  citations: Citation[];
  web_sources?: WebSource[];
  traces: Trace[];
  tool_events?: ToolEvent[];
  display_blocks?: DisplayBlock[];
  refused: boolean;
  error_detail?: string | null;
  feedback?: Feedback | null;
  provider_name?: string | null;
  site_provider_id?: string | null;
  model?: string | null;
  thinking?: { label: string; option_id: string | null; config_version: number; kind: string } | null;
};
type ModelChoice = {
  id: string;
  name: string;
  model: string;
  available: boolean;
  supports_images?: boolean;
  supports_tools?: boolean;
  unavailable_reason?: "model_disabled" | "connection_disabled" | "pricing_unconfigured" | "service_not_ready" | null;
  thinking?: ThinkingCapability;
  context_capacity: number | null;
  max_output_tokens: number | null;
};
type ModelOptions = { default_provider_id: string | null; providers: ModelChoice[] };
type ContextResponse = {
  estimated_input_tokens: number; capacity: number | null; output_reserve: number | null;
  eligible: boolean; status: ConversationContextState["status"];
  window_id?: string | null; context_revision?: number;
};
const emptyContext: ConversationContextState = {
  estimatedInputTokens: null, capacity: null, outputReserve: null, eligible: false, status: "idle",
};
function contextState(value: ContextResponse): ConversationContextState {
  return { estimatedInputTokens: value.estimated_input_tokens, capacity: value.capacity,
    outputReserve: value.output_reserve, eligible: value.eligible, status: value.status,
    windowId: value.window_id, contextRevision: value.context_revision };
}
type Conversation = {
  id: string;
  title: string;
  revision: number;
  title_source?: string;
  title_task_status?: string | null;
  pinned_at?: string | null;
  knowledge_bases: { id: string; name: string }[];
  active_request_id: string | null;
  updated_at: string;
  messages: Message[];
  compactions?: { id: string; after_sequence: number; created_at: string }[];
  skill_ids: string[];
  mcp_server_ids: string[];
  question_workbench: boolean;
};
type Generation = {
  controller: AbortController;
  conversationId: string;
  requestId: string;
  epoch: number;
};

function upsert(messages: Message[], message: Message) {
  const previous = messages.find(item => item.id === message.id && item.request_id === message.request_id);
  if (previous?.display_blocks && message.display_blocks) {
    message = { ...message, display_blocks: message.display_blocks.map(block => {
      const live = previous.display_blocks?.find(item => item.id === block.id);
      return block.type === "thinking" && live?.type === "thinking" && live.text !== undefined
        ? { ...block, text: live.text } : block;
    }) };
  }
  return [...messages.filter((item) => item.id !== message.id), message].sort(
    (a, b) => a.sequence - b.sequence,
  );
}
type DraftMention = MentionReference & { start: number; end: number };
type PositionedMention = MentionReference & { start?: number; end?: number };
function storedMentions(message: Pick<Message, "document_mentions" | "knowledge_base_mentions" | "resource_mentions">): PositionedMention[] {
  return [
    ...(message.knowledge_base_mentions ?? []).map(item => ({ ...item, kind: "knowledge_base" as const })),
    ...(message.document_mentions ?? []).map(item => ({ ...item, kind: "document" as const })),
    ...(message.resource_mentions ?? []).map(item => ({ ...item, kind: "resource" as const })),
  ];
}
function mentionToken(item: MentionReference) {
  return item.kind === "knowledge_base" ? `@知识库/${item.name}`
    : item.kind === "resource" ? `@资源/${item.path}`
    : `@${item.filename}（${item.knowledge_base_name ?? "知识库"}）`;
}
function mentionTokens(item: MentionReference) {
  return item.kind !== "document" ? [mentionToken(item)]
    : [...new Set([mentionToken(item), `@${item.filename}`])];
}
function mentionRanges(text: string, mentions: PositionedMention[]) {
  if (mentions.length && mentions.every(item => item.start !== undefined && item.end !== undefined)) {
    const parts: { text: string; mention?: MentionReference }[] = [];
    let cursor = 0;
    for (const mention of [...mentions].sort((a, b) => a.start! - b.start!)) {
      if (mention.start! > cursor) parts.push({ text: text.slice(cursor, mention.start) });
      parts.push({ text: text.slice(mention.start, mention.end), mention });
      cursor = mention.end!;
    }
    parts.push({ text: text.slice(cursor) });
    return parts;
  }
  const remaining = [...mentions];
  const parts: { text: string; mention?: MentionReference }[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    let found: { index: number; mention: MentionReference; marker: string } | undefined;
    for (const mention of remaining) {
      for (const marker of mentionTokens(mention)) {
        const index = text.indexOf(marker, cursor);
        if (index >= 0 && (!found || index < found.index
            || (index === found.index && marker.length > found.marker.length))) {
          found = { index, mention, marker };
        }
      }
    }
    if (!found) break;
    if (found.index > cursor) parts.push({ text: text.slice(cursor, found.index) });
    parts.push({ text: found.marker, mention: found.mention });
    cursor = found.index + found.marker.length;
    remaining.splice(remaining.indexOf(found.mention), 1);
  }
  if (cursor < text.length || !parts.length) parts.push({ text: text.slice(cursor) });
  return parts;
}
function pruneMentions(previous: string, text: string, caret: number, mentions: DraftMention[]) {
  if (previous === text) return mentions;
  let start = 0;
  while (start < Math.min(previous.length, text.length, caret) && previous[start] === text[start]) start++;
  let suffix = 0;
  while (suffix < Math.min(previous.length - start, text.length - start)
    && previous[previous.length - suffix - 1] === text[text.length - suffix - 1]) suffix++;
  const oldEnd = previous.length - suffix;
  const shift = text.length - previous.length;
  return mentions.flatMap(mention => {
    if (mention.end <= start) return [mention];
    if (mention.start >= oldEnd) return [{ ...mention, start: mention.start + shift, end: mention.end + shift }];
    return [];
  }).filter(mention => mentionTokens(mention).some(token =>
    text.slice(mention.start, mention.end) === token));
}
function AnswerMarkdown({
  message,
  onCitation,
  text,
}: {
  message: Message;
  onCitation: (item: Citation) => void;
  text?: string;
}) {
  const footnotePrefix = `footnote-${useId()}-`;
  return (
    <div className="answer-text">
      <ReactMarkdown
        skipHtml
        remarkRehypeOptions={{ clobberPrefix: footnotePrefix, footnoteLabel: "注释", footnoteBackLabel: "返回正文" }}
        remarkPlugins={[remarkGfm, citationMarkers([...message.citations, ...(message.web_sources ?? [])])]}
        components={{
          table: ({ children }) => <div className="answer-table-scroll" role="region" aria-label="回答表格" tabIndex={0}><table>{children}</table></div>,
          img: ({ alt }) => <span>{alt}</span>,
          a: ({ href, children, node: _node, ...props }) => {
            const web = message.web_sources?.find(item => href === `#source-${item.source_id}`);
            if (web) return <a className="inline-citation" href={web.url} target="_blank" rel="noopener noreferrer" title={`查看网页：${web.title}`}>{children}</a>;
            const citation = message.citations.find(
              (item) => href === `#source-${item.chunk_id}`,
            );
            if (!citation && href?.startsWith("#")) return <a {...props} href={href}>{children}</a>;
            return citation ? (
              <button
                className="inline-citation"
                title={`查看来源：${citation.filename}`}
                aria-label={`查看来源：${citation.filename}`}
                onClick={() => onCitation(citation)}
              >
                {children}
              </button>
            ) : (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {text ?? message.content}
      </ReactMarkdown>
    </div>
  );
}

export function ChatWorkspace({
  libraries,
  conversationId,
}: {
  libraries: Library[];
  conversationId: string | null;
}) {
  const router = useRouter();
  const { session } = useAuth();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [draftExtensions, setDraftExtensions] = useState<ExtensionSelection | null>(null);
  const [question, setQuestion] = useState("");
  const [documentMentions, setDocumentMentions] = useState<DraftMention[]>([]);
  const [caret, setCaret] = useState(0);
  const [extensionBusy, setExtensionBusy] = useState(false);
  const [slashState, setSlashState] = useState<{ open: boolean; option?: string }>({ open: false });
  const [mentionState, setMentionState] = useState<{ open: boolean; option?: string }>({ open: false });
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const slashMenu = useRef<SlashMenuHandle>(null);
  const mentionMenu = useRef<MentionMenuHandle>(null);
  const updateSlashState = useCallback((open: boolean, option?: string) => setSlashState(current =>
    current.open === open && current.option === option ? current : { open, option }), []);
  const updateMentionState = useCallback((open: boolean, option?: string) => setMentionState(current =>
    current.open === open && current.option === option ? current : { open, option }), []);
  const attachments = useChatAttachments();
  const [dragging, setDragging] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelOptions | null>(null);
  const [modelId, setModelId] = useState("");
  const [thinkingId, setThinkingId] = useState("");
  const [thinkingVersion, setThinkingVersion] = useState(0);
  const [thinkingStale, setThinkingStale] = useState(false);
  const [modelError, setModelError] = useState("");
  const [modelLoading, setModelLoading] = useState(true);
  const [modelAttempt, setModelAttempt] = useState(0);
  const [webEnabled, setWebEnabled] = useState(true);
  const [webAvailable, setWebAvailable] = useState(false);
  const [webFailed, setWebFailed] = useState(false);
  const [webAttempt, setWebAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setWebFailed(false);
    request<{ available: boolean }>("/search/availability", { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setWebAvailable(value.available); })
      .catch(() => { if (!controller.signal.aborted) { setWebAvailable(false); setWebFailed(true); } });
    return () => controller.abort();
  }, [webAttempt, session.workspace_id]);
  useEffect(() => { setWebEnabled(true); }, [session.workspace_id]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [loading, setLoading] = useState(false);
  const [conversationError, setConversationError] = useState("");
  const [reloadAttempt, setReloadAttempt] = useState(0);
  const [asking, setAsking] = useState(false);
  const [phase, setPhase] = useState("");
  const [contextInfo, setContextInfo] = useState<ConversationContextState>(emptyContext);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState("");
  const [contextAttempt, setContextAttempt] = useState(0);
  const [compactRequest, setCompactRequest] = useState(0);
  const [latest, setLatest] = useState(false);
  const setSidebar = useUiStore((state) => state.setSidebarOpen);
  const [notice, setNotice] = useState<{
    text: string;
    error?: boolean;
  } | null>(null);
  const [preview, setPreview] = useState<Citation | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [editing, setEditing] = useState(false);
  const [renameTarget, setRenameTarget] = useState<HistoryItem | null>(null);
  const [title, setTitle] = useState("");
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [modalBusy, setModalBusy] = useState(false);
  const [modalError, setModalError] = useState("");
  const generation = useRef<Generation | null>(null);
  const epoch = useRef(0);
  const viewRef = useRef<Conversation | null>(null);
  const sending = useRef(false);
  const scrollArea = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<{ id: string; top: number } | null>(null);
  const follow = useRef(true);
  const restoredModelFor = useRef<string | null>(null);
  const cache = useRef(new Map<string, Conversation>());
  const draftPrefix = `rag-chat-draft:${session.user.id}:${session.workspace_id}:`;
  const draftKey = useRef(draftPrefix + (conversationId ?? "new"));
  const draftReady = useRef(false);
  const draftSending = useRef(false);
  const cacheScope = useRef(draftPrefix);
  const historyScope = useRef(draftPrefix);
  historyScope.current = draftPrefix;
  const historyEpoch = useRef(0);
  const liveDraft = useRef({ text: question, mentions: documentMentions, attachments: attachments.ready, scroll: 0, follow: true });
  liveDraft.current = { text: question, mentions: documentMentions,
    attachments: attachments.ready.filter(item => !!item.resource_id), scroll: scrollArea.current?.scrollTop ?? 0, follow: follow.current };
  const searchParams = useSearchParams();
  const targetMessage = searchParams.get("message");
  const [highlightedMessage, setHighlightedMessage] = useState<string | null>(null);
  function saveDraft() {
    if (!draftReady.current) return;
    liveDraft.current = { ...liveDraft.current, scroll: scrollArea.current?.scrollTop ?? liveDraft.current.scroll,
      follow: follow.current };
    if (!draftSending.current) try { sessionStorage.setItem(draftKey.current, JSON.stringify(liveDraft.current)); } catch { /* Full storage must not block chatting. */ }
    if (viewRef.current) cache.current.set(viewRef.current.id, viewRef.current);
    if (cache.current.size > 20) cache.current.delete(cache.current.keys().next().value!);
  }
  const persistDraft = useRef(saveDraft);
  persistDraft.current = saveDraft;
  useEffect(() => {
    if (draftKey.current === draftPrefix + (conversationId ?? "new")) saveDraft();
  }, [question, documentMentions, attachments.ready.map(item => item.id).join(",")]);
  useEffect(() => {
    const save = () => persistDraft.current();
    window.addEventListener("pagehide", save);
    const clear = () => {
      draftReady.current = false;
      cache.current.clear();
      for (let index = sessionStorage.length - 1; index >= 0; index--) {
        const key = sessionStorage.key(index); if (key?.startsWith("rag-chat-draft:")) sessionStorage.removeItem(key);
      }
    };
    window.addEventListener("rag-session-expired", clear);
    window.addEventListener("rag-session-ended", clear);
    return () => { save(); window.removeEventListener("pagehide", save); window.removeEventListener("rag-session-expired", clear); window.removeEventListener("rag-session-ended", clear); };
  }, []);
  viewRef.current = conversation;
  const selectedModel = modelOptions?.providers.find(item => item.id === modelId);
  const thinking = selectedModel?.thinking;
  const activeComposerMenu = mentionState.open ? mentionState : slashState;
  useEffect(() => {
    const input = composerInput.current;
    const overlay = input?.parentElement?.querySelector<HTMLElement>(".composer-mention-overlay");
    if (!input || !overlay) return;
    const sync = () => {
      overlay.style.width = `${input.clientWidth}px`;
      overlay.style.height = `${input.clientHeight}px`;
      overlay.scrollTop = input.scrollTop;
    };
    const observer = new ResizeObserver(sync);
    observer.observe(input);
    sync();
    return () => observer.disconnect();
  }, [documentMentions.length]);
  useEffect(() => {
    const controller = new AbortController();
    setContextError("");
    if (!conversation?.id || !selectedModel) {
      setContextLoading(false);
      setContextInfo({ ...emptyContext, capacity: selectedModel?.context_capacity ?? null,
        outputReserve: selectedModel?.max_output_tokens ?? null });
      return;
    }
    setContextInfo(emptyContext);
    setContextLoading(true);
    const params = new URLSearchParams({ provider_id: modelId, thinking_config_version: String(thinkingVersion) });
    params.set("web_search", String(webEnabled && webAvailable));
    if (thinkingId) params.set("thinking_option_id", thinkingId);
    request<ContextResponse>(`/conversations/${conversation.id}/context?${params}`,
      { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted) setContextInfo(current => acceptContextState(current, contextState(value)));
      }).catch(error => {
        if (!controller.signal.aborted) setContextError(
          error instanceof Error ? error.message : "上下文状态加载失败");
      }).finally(() => { if (!controller.signal.aborted) setContextLoading(false); });
    return () => controller.abort();
  }, [conversation?.id, modelId, selectedModel, thinkingId, thinkingVersion, contextAttempt, webEnabled, webAvailable]);

  const report = useCallback((error: unknown) => {
    setNotice({
      text: error instanceof Error ? error.message : "请求失败，请重试",
      error: true,
    });
  }, []);
  const refreshList = useCallback(async (cursor?: string | null) => {
    const currentEpoch = ++historyEpoch.current;
    const scope = `rag-chat-draft:${session.user.id}:${session.workspace_id}:`;
    setHistoryError("");
    setHistoryLoading(true);
    try {
      const page = await request<{ items: Conversation[]; next_cursor: string | null }>(`/conversations${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (historyEpoch.current !== currentEpoch || historyScope.current !== scope) return;
      setConversations(current => cursor ? [...current, ...page.items.filter(item => !current.some(row => row.id === item.id))] : page.items);
      setHistoryCursor(page.next_cursor);
    } catch (error) {
      if (historyEpoch.current === currentEpoch && historyScope.current === scope)
        setHistoryError(error instanceof Error ? error.message : "对话加载失败");
    } finally {
      if (historyEpoch.current === currentEpoch && historyScope.current === scope) setHistoryLoading(false);
    }
  }, [session.workspace_id, session.user.id]);
  useEffect(() => {
    if (!conversation) return;
    setConversations(items => items.map(item => item.id === conversation.id ? { ...item,
      title: conversation.title, title_source: conversation.title_source, revision: conversation.revision,
      pinned_at: conversation.pinned_at, active_request_id: conversation.active_request_id,
      updated_at: conversation.updated_at } : item));
  }, [conversation?.id, conversation?.title, conversation?.revision, conversation?.active_request_id]);
  const backgroundRequests = conversations.filter(item => item.active_request_id && item.id !== conversation?.id)
    .map(item => item.id).sort().join(",");
  useEffect(() => {
    if (!backgroundRequests) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      for (const id of backgroundRequests.split(",")) {
        void request<Conversation>(`/conversations/${id}`, { signal: controller.signal }).then(item => {
          if (controller.signal.aborted) return;
          cache.current.set(item.id, item);
          setConversations(items => items.map(row => row.id === id ? { ...row, ...item } : row));
        }).catch(error => {
          if (!controller.signal.aborted && error instanceof ApiError && [403, 404].includes(error.status)) {
            cache.current.delete(id); setConversations(items => items.filter(row => row.id !== id));
          }
        });
      }
    }, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [backgroundRequests, session.workspace_id]);
  const stopGeneration = useCallback(async () => {
    const active = generation.current;
    if (!active) return;
    active.controller.abort();
    await request(`/conversations/${active.conversationId}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: active.requestId }),
    });
  }, []);
  useEffect(() => {
    void refreshList();
    return () => {
      epoch.current++;
      generation.current?.controller.abort();
    };
  }, [refreshList, stopGeneration]);
  useEffect(() => {
    const controller = new AbortController();
    setModelLoading(true);
    setModelError("");
    request<ModelOptions>("/models/available", { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) {
        setModelOptions(data);
        setModelId(current => data.providers.some(item => item.id === current && item.available) ? current
          : data.providers.find(item => item.id === data.default_provider_id && item.available)?.id
            ?? data.providers.find(item => item.available)?.id ?? "");
      }
    }).catch(error => {
      if (!controller.signal.aborted) setModelError(error instanceof Error ? error.message : "模型加载失败");
    }).finally(() => { if (!controller.signal.aborted) setModelLoading(false); });
    return () => controller.abort();
  }, [modelAttempt]);
  useEffect(() => {
    setThinkingId(thinking?.default_option_id ?? "");
    setThinkingVersion(thinking?.version ?? 0);
  }, [modelId, thinking?.version, thinking?.default_option_id]);
  useLayoutEffect(() => {
    // Swap the body before paint; a route change must never display another chat.
    if (conversationId && viewRef.current?.id === conversationId && cacheScope.current === draftPrefix) return;
    const controller = new AbortController();
    const currentEpoch = ++epoch.current;
    saveDraft();
    if (cacheScope.current !== draftPrefix) { cache.current.clear(); setConversations([]); cacheScope.current = draftPrefix; }
    draftReady.current = false;
    draftSending.current = false;
    generation.current?.controller.abort();
    generation.current = null;
    sending.current = false;
    setAsking(false);
    draftKey.current = draftPrefix + (conversationId ?? "new");
    let draft: typeof liveDraft.current | null = null;
    try { draft = JSON.parse(sessionStorage.getItem(draftKey.current) ?? "null"); } catch { /* Ignore malformed local drafts. */ }
    // Persist the restored snapshot while React applies the matching input state.
    liveDraft.current = { text: typeof draft?.text === "string" ? draft.text : "",
      mentions: Array.isArray(draft?.mentions) ? draft.mentions : [],
      attachments: Array.isArray(draft?.attachments) ? draft.attachments : [],
      scroll: draft?.scroll ?? 0, follow: draft?.follow ?? true };
    setQuestion(typeof draft?.text === "string" ? draft.text : "");
    setDocumentMentions(Array.isArray(draft?.mentions) ? draft.mentions : []);
    attachments.restore(Array.isArray(draft?.attachments) ? draft.attachments : []);
    draftReady.current = true;
    setPreview(null);
    setTrace(null);
    const cached = conversationId ? cache.current.get(conversationId) : null;
    setConversation(cached ?? null);
    setConversationError("");
    follow.current = !targetMessage && (draft?.follow ?? !draft);
    const restoreScroll = draft?.scroll ?? 0;
    pendingScroll.current = conversationId && !targetMessage ? { id: conversationId, top: restoreScroll } : null;
    setLoading(!!conversationId && !cached);
    setEditing(false); setRenameTarget(null);
    if (conversationId) void request<Conversation>(`/conversations/${encodeURIComponent(conversationId)}`, { signal: controller.signal })
      .then(item => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) {
          cache.current.set(item.id, item); setConversation(item);
        }
      })
      .catch(error => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) {
          cache.current.delete(conversationId!); setConversation(null);
          setConversationError(error instanceof Error ? error.message : "对话加载失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) setLoading(false);
      });
    return () => controller.abort();
  }, [conversationId, reloadAttempt, draftPrefix]);
  useLayoutEffect(() => {
    const position = pendingScroll.current;
    const area = scrollArea.current;
    if (!position || position.id !== conversation?.id || !area) return;
    area.scrollTo({ top: position.top || (follow.current ? area.scrollHeight : 0), behavior: "instant" });
    pendingScroll.current = null;
  }, [conversation?.id, loading]);
  useEffect(() => {
    if (loading || !targetMessage || conversation?.id !== conversationId) return;
    follow.current = false;
    const frame = requestAnimationFrame(() => {
      const target = document.getElementById(`chat-message-${targetMessage}`);
      if (target) { target.scrollIntoView({ block: "center", behavior: "instant" }); setHighlightedMessage(targetMessage); }
    });
    const timer = setTimeout(() => setHighlightedMessage(null), 4500);
    return () => { cancelAnimationFrame(frame); clearTimeout(timer); };
  }, [targetMessage, conversation?.id, conversationId, loading]);
  useEffect(() => {
    if (!modelOptions || loading || (conversationId && conversation?.id !== conversationId)) return;
    const target = conversationId ?? "new";
    if (restoredModelFor.current === target) return;
    restoredModelFor.current = target;
    const previous = conversation?.messages.findLast(item => item.role === "assistant" && item.site_provider_id)?.site_provider_id;
    const available = modelOptions.providers.filter(item => item.available);
    setModelId(current => {
      const restored = available.find(item => item.id === previous);
      const fallback = available.find(item => item.id === modelOptions.default_provider_id)
        ?? available.find(item => item.id === current) ?? available[0];
      return restored?.id ?? (previous ? fallback?.id : available.find(item => item.id === current)?.id ?? fallback?.id) ?? "";
    });
    if (previous && !available.some(item => item.id === previous)) {
      setNotice({ text: available.length ? "之前的模型暂不可用，已选择其他可用模型" : "之前的模型暂不可用，请稍后重试" });
    }
  }, [conversationId, conversation, loading, modelOptions]);
  useEffect(() => {
    const area = scrollArea.current;
    if (area && follow.current) area.scrollTop = area.scrollHeight;
  }, [conversation?.messages, asking, phase]);
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(timer);
  }, [notice]);
  // A refreshed page can inspect and stop the already-running server request.
  useEffect(() => {
    if (!conversation?.active_request_id || asking) return;
    const id = conversation.id;
    const controller = new AbortController();
    let pending: ReturnType<typeof setTimeout> | undefined;
    const reload = () => {
      request<Conversation>(`/conversations/${id}`)
        .then((item) => {
          if (!controller.signal.aborted && viewRef.current?.id === id && !generation.current)
            setConversation(item);
        })
        .catch(error => {
          if (!controller.signal.aborted && viewRef.current?.id === id && error instanceof ApiError && [403, 404].includes(error.status)) {
            cache.current.delete(id); setConversation(null); setConversationError(error.message);
          }
        });
    };
    void streamRequest(`/conversations/${id}/requests/${conversation.active_request_id}/events`, undefined, controller.signal, () => {
      if (!pending) pending = setTimeout(() => { pending = undefined; reload(); }, 250);
    }).catch(() => { if (!controller.signal.aborted) reload(); });
    const timer = setInterval(reload, 1500);
    return () => { controller.abort(); clearInterval(timer); if (pending) clearTimeout(pending); };
  }, [conversation?.id, conversation?.active_request_id, asking]);
  useEffect(() => {
    if (!conversation || conversation.title_source !== "temporary" || conversation.active_request_id
        || !conversation.messages.some(item => item.role === "assistant" && item.status === "completed")
        || (conversation.title_task_status && !["queued", "running"].includes(conversation.title_task_status))) return;
    const controller = new AbortController();
    let count = 0;
    const timer = setInterval(() => {
      if (++count > 30) { clearInterval(timer); return; }
      void request<Conversation>(`/conversations/${conversation.id}`, { signal: controller.signal }).then(item => {
        if (!controller.signal.aborted && viewRef.current?.id === item.id) {
          setConversation(current => current && ({ ...current, title: item.title, revision: item.revision,
            title_source: item.title_source, title_task_status: item.title_task_status }));
          if (item.title_source !== "temporary") void refreshList();
        }
      }).catch(() => {});
    }, 2000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [conversation?.id, conversation?.title_source, conversation?.title_task_status, conversation?.active_request_id, asking, refreshList]);

  function newChat() {
    saveDraft();
    setDraftExtensions(null);
    ++epoch.current;
    generation.current?.controller.abort();
    generation.current = null;
    sending.current = false;
    attachments.reset();
    setConversation(null);
    setConversationError("");
    setLoading(false);
    setQuestion("");
    setDocumentMentions([]);
    draftKey.current = draftPrefix + "new";
    liveDraft.current = { text: "", mentions: [], attachments: [], scroll: 0, follow: true };
    try { sessionStorage.removeItem(draftKey.current); } catch { /* Optional persistence. */ }
    setAsking(false);
    setSidebar(false);
    setPreview(null);
    setTrace(null);
    follow.current = true;
    if (conversationId) router.push("/?chat=1", { scroll: false });
    requestAnimationFrame(() => composerInput.current?.focus());
  }
  async function reloadCurrent(id: string, currentEpoch: number) {
    try {
      const saved = await request<Conversation>(`/conversations/${id}`);
      if (epoch.current === currentEpoch) setConversation(current => ({ ...saved,
        messages: saved.messages.reduce((messages, message) => upsert(messages, message),
          current?.id === id ? current.messages.filter(item => saved.messages.some(row => row.id === item.id)) : []),
      }));
      return saved;
    } catch (error) {
      if (epoch.current === currentEpoch) report(error);
    }
  }
  async function stop() {
    if (generation.current) {
      const active = generation.current;
      try {
        await stopGeneration();
        await reloadCurrent(active.conversationId, active.epoch);
      } catch (error) {
        report(error);
      }
    } else if (conversation?.active_request_id) {
      const id = conversation.id;
      try {
        await request(`/conversations/${id}/stop`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: conversation.active_request_id }),
        });
        await reloadCurrent(id, epoch.current);
      } catch (error) {
        report(error);
      }
    }
  }
  async function changeExtensions(value: ExtensionSelection) {
    if (!conversation) { setDraftExtensions(value); return; }
    const id = conversation.id;
    setExtensionBusy(true);
    try {
      const updated = await request<Conversation>(`/conversations/${id}/extensions`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
      });
      setConversation(current => current?.id === id ? { ...current, skill_ids: updated.skill_ids, mcp_server_ids: updated.mcp_server_ids, question_workbench: updated.question_workbench } : current);
    } finally { setExtensionBusy(false); }
  }

  async function send(retryId?: string) {
    if (
      sending.current || extensionBusy ||
      modelLoading || !!modelError || thinkingStale || !modelOptions?.providers.some(item => item.id === modelId && item.available) ||
      loading ||
      (!retryId && (!question.trim() && !attachments.ready.length || attachments.blocked ||
        attachments.hasImages && !selectedModel?.supports_images)) ||
      conversation?.active_request_id
    )
      return;
    sending.current = true;
    setAsking(true);
    setPhase("正在分析问题…");
    const currentEpoch = epoch.current;
    const query = question.trim();
    const leading = question.length - question.trimStart().length;
    const turnDraftMentions = documentMentions.map(item => ({ ...item,
      start: item.start - leading, end: item.end - leading }));
    const turnMentions = [...turnDraftMentions].sort((a, b) => a.start - b.start)
      .map(({ start: _start, end: _end, ...item }) => item);
    const turnDocumentMentions = turnMentions.filter((item): item is Extract<MentionReference, { kind: "document" }> => item.kind === "document");
    const turnKnowledgeBaseMentions = turnMentions.filter((item): item is Extract<MentionReference, { kind: "knowledge_base" }> => item.kind === "knowledge_base");
    const turnResourceMentions = turnMentions.filter((item): item is Extract<MentionReference, { kind: "resource" }> => item.kind === "resource");
    const controller = new AbortController();
    let current = conversation;
    let active: Generation | null = null;
    let received = false;
    try {
      if (!current) {
        current = await request<Conversation>("/conversations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...(draftExtensions ?? {}) }),
          signal: controller.signal,
        });
        if (epoch.current !== currentEpoch) return;
        setConversation(current);
        viewRef.current = current;
        try { sessionStorage.setItem(draftPrefix + current.id, JSON.stringify(liveDraft.current)); } catch { /* Optional persistence. */ }
        try { sessionStorage.removeItem(draftPrefix + "new"); } catch { /* Optional persistence. */ }
        draftKey.current = draftPrefix + current.id;
        router.replace(`/?conversation=${encodeURIComponent(current.id)}`, {
          scroll: false,
        });
      }
      active = {
        controller,
        conversationId: current.id,
        requestId: crypto.randomUUID(),
        epoch: currentEpoch,
      };
      generation.current = active;
      draftSending.current = true;
      setQuestion("");
      follow.current = true;
      const pendingUser: Message = {
        id: `pending-${active.requestId}`,
        sequence: (current.messages.at(-1)?.sequence ?? -1) + 1,
        role: "user",
        content: query,
        attachments: attachments.ready,
        document_mentions: turnDocumentMentions,
        knowledge_base_mentions: turnKnowledgeBaseMentions,
        resource_mentions: turnResourceMentions,
        status: "completed",
        citations: [],
        traces: [],
        refused: false,
      };
      setConversation({
        ...current,
        messages: retryId
          ? current.messages.map((item) =>
              item.id === retryId
                ? {
                    ...item,
                    content: "",
                    status: "streaming",
                    error_detail: null,
                    citations: [],
                    traces: [], web_sources: [], tool_events: [], display_blocks: [],
                  }
                : item,
            )
          : [...current.messages, pendingUser],
      });
      await streamRequest(
        `/conversations/${current.id}/messages`,
        {
          request_id: active.requestId,
          provider_id: modelId,
          web_search: webEnabled && webAvailable,
          thinking_config_version: thinkingVersion,
          document_ids: turnDocumentMentions.map(item => item.id),
          knowledge_base_ids: turnKnowledgeBaseMentions.map(item => item.id),
          resource_ids: turnResourceMentions.map(item => item.id),
          ...(thinkingId ? { thinking_option_id: thinkingId } : {}),
          ...(retryId ? { retry_message_id: retryId } : {
            ...(query ? { query } : {}), attachment_ids: attachments.ready.map(item => item.id),
          }),
        },
        controller.signal,
        (name, data) => {
          if (name === "submitted") {
            received = true;
            try { sessionStorage.removeItem(draftPrefix + current!.id); } catch { /* Optional persistence. */ }
          }
          if (epoch.current !== currentEpoch || controller.signal.aborted)
            return;
          if (name === "submitted") {
            draftSending.current = false;
            setDocumentMentions([]);
            if (!retryId) attachments.reset(false);
          }
          if (name === "phase")
            setPhase(
              (data as { phase: string }).phase === "planning"
                ? "正在分析问题…"
                : (data as { phase: string }).phase === "retrieving"
                  ? "正在检索资料…"
                  : "正在生成回答…",
            );
          if (name === "agent_step")
            setPhase(
              (data as { status?: string }).status === "decided"
                ? "Agent 正在整理检索结果…"
                : "正在分析问题…",
            );
          if (name === "tool_started")
            setPhase(`正在执行${(data as { label?: string }).label ?? "工具"}…`);
          if (name === "tool_result") setPhase("正在分析工具结果…");
          if (name === "thinking_delta") setPhase("正在思考…");
          if (name === "thinking_status" || name === "delta") setPhase("正在生成回答…");
          if (name === "context") setContextInfo(current => acceptContextState(current, contextState(data as ContextResponse)));
          if (name === "context_error") setContextInfo(value => ({ ...value, status: "failed", error: (data as { message: string }).message }));
          if (name === "compaction") {
            const status = (data as ToolEvent).status;
            const update = data as { window_id?: string; context_revision?: number; summary?: string };
            setContextInfo(value => acceptContextState(value, { ...value,
              status: status === "running" ? "running" : status === "completed" ? "completed" : "failed",
              error: status === "failed" ? update.summary : undefined,
              windowId: update.window_id ?? value.windowId, contextRevision: update.context_revision ?? value.contextRevision }));
            setPhase(status === "running" ? "正在整理早期上下文…" : "正在继续回答…");
          }
          if (name === "message" || name === "done") {
            received = true;
            draftSending.current = false;
            try { sessionStorage.removeItem(draftPrefix + current!.id); } catch { /* Optional persistence. */ }
            setDocumentMentions([]);
            if (!retryId) attachments.reset(false);
          }
          setConversation((item) => {
            if (!item || item.id !== current!.id) return item;
            if (name === "message" || name === "done") {
              return {
                ...item,
                messages: upsert(item.messages, data as Message),
                active_request_id: name === "done" ? null : active!.requestId,
              };
            }
            const messages = item.messages.map((message) => {
              if (message.request_id !== active!.requestId) return message;
              if (name === "thinking_delta" || name === "thinking_status") {
                return { ...message, display_blocks: updateThinking(message.display_blocks ?? [],
                  data as { block_id: string; round: number; text?: string; status?: string }) };
              }
              if (name === "delta") {
                const delta = data as { text: string; block_id: string };
                const blocks = message.display_blocks ?? [];
                return {
                  ...message,
                  content: message.content + (blocks.some(block => block.id === delta.block_id) || !message.content ? "" : "\n\n") + delta.text,
                  display_blocks: appendText(blocks, delta.block_id, delta.text),
                };
              }
              if (["tool_call", "tool_started", "tool_result", "compaction"].includes(name)) {
                const update = data as ToolEvent;
                const tools = message.tool_events ?? [];
                const existing = tools.find(tool => tool.call_id === update.call_id);
                const next = { ...existing, ...update, ...(name === "compaction" ? { kind: "compaction", name: "compact_context" } : {}) };
                return { ...message, tool_events: existing
                  ? tools.map(tool => tool.call_id === update.call_id ? next : tool) : [...tools, next],
                  display_blocks: appendActivity(message.display_blocks ?? [], update.call_id) };
              }
              if (name === "web_sources") return { ...message, web_sources: (data as { sources: WebSource[] }).sources };
              if (name === "sources")
                return {
                  ...message,
                  ...(data as { citations: Citation[]; traces: Trace[] }),
                };
              return message;
            });
            return { ...item, messages };
          });
        },
      );
    } catch (error) {
      if (!controller.signal.aborted && epoch.current === currentEpoch) {
        if (error instanceof ApiError && error.status === 409 && error.message.includes("思考配置")) {
          setThinkingStale(true);
          setModelAttempt(value => value + 1);
        }
        report(error);
        if (!received && !retryId) { setQuestion(query); setDocumentMentions(turnDraftMentions); }
      }
    } finally {
      if (epoch.current === currentEpoch) {
        if (current) {
          const saved = await reloadCurrent(current.id, currentEpoch);
          const acceptedRequestId = active?.requestId;
          if (!retryId && !received && acceptedRequestId && saved?.messages.some(message => message.request_id === acceptedRequestId)) {
            attachments.reset(false);
            setQuestion("");
          }
        }
        generation.current = null;
        draftSending.current = false;
        sending.current = false;
        setAsking(false);
        setPhase("");
        setContextAttempt(value => value + 1);
      }
      void refreshList();
    }
  }
  async function compact() {
    if (!conversation || sending.current || conversation.active_request_id || !selectedModel?.available || thinkingStale) return;
    const currentEpoch = epoch.current;
    const active: Generation = { controller: new AbortController(), conversationId: conversation.id,
      requestId: crypto.randomUUID(), epoch: currentEpoch };
    generation.current = active;
    sending.current = true;
    setAsking(true);
    setPhase("正在整理早期上下文…");
    setContextInfo(value => ({ ...value, status: "running" }));
    try {
      await streamRequest(`/conversations/${conversation.id}/compact`, {
        request_id: active.requestId, provider_id: modelId, thinking_config_version: thinkingVersion,
        ...(thinkingId ? { thinking_option_id: thinkingId } : {}),
      }, active.controller.signal, (name, data) => {
        if (epoch.current !== currentEpoch || active.controller.signal.aborted) return;
        if (name === "compaction") {
          const status = (data as ToolEvent).status;
          const update = data as { window_id?: string; context_revision?: number; summary?: string };
          setContextInfo(value => acceptContextState(value, { ...value,
            status: status === "running" ? "running" : status === "completed" ? "completed" : "failed",
            error: status === "failed" ? update.summary : undefined,
            windowId: update.window_id ?? value.windowId, contextRevision: update.context_revision ?? value.contextRevision }));
        }
        if (name === "context") setContextInfo(current => acceptContextState(current, contextState(data as ContextResponse)));
        if (name === "done") {
          const result = data as { status: string; error?: string };
          if (result.status === "failed") throw new Error(result.error || "整理未完成，原始上下文已保留");
        }
      });
    } catch (error) {
      if (!active.controller.signal.aborted && epoch.current === currentEpoch) {
        setContextInfo(value => ({ ...value, status: "failed", error: error instanceof Error ? error.message : "整理失败" }));
        report(error);
      }
    } finally {
      if (epoch.current === currentEpoch) {
        generation.current = null; sending.current = false; setAsking(false); setPhase("");
        setContextAttempt(value => value + 1);
        await reloadCurrent(active.conversationId, currentEpoch);
      }
    }
  }
  async function rename(event?: FormEvent) {
    event?.preventDefault();
    const target = renameTarget ?? conversation;
    if (!target) return;
    setModalBusy(true);
    setModalError("");
    try {
      const updated = await request<Conversation>(
        `/conversations/${target.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.trim(), revision: target.revision }),
        },
      );
      setConversation((item) =>
        item?.id === updated.id ? { ...item, ...updated } : item,
      );
      cache.current.delete(target.id);
      setEditing(false);
      void refreshList();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "重命名失败");
    } finally {
      setModalBusy(false);
    }
  }
  async function pin(item: HistoryItem) {
    try {
      const updated = await request<Conversation>(`/conversations/${item.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned: !item.pinned_at, revision: item.revision }),
      });
      cache.current.delete(item.id);
      setConversation(current => current?.id === item.id ? { ...current, ...updated } : current);
      void refreshList();
    } catch (error) { report(error); }
  }
  async function remove() {
    if (!deleting) return;
    setModalBusy(true);
    setModalError("");
    try {
      await request(`/conversations/${deleting.id}`, { method: "DELETE" });
      cache.current.delete(deleting.id);
      try { sessionStorage.removeItem(draftPrefix + deleting.id); } catch { /* Optional persistence. */ }
      if (conversation?.id === deleting.id) newChat();
      setDeleting(null);
      void refreshList();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "删除失败");
    } finally {
      setModalBusy(false);
    }
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNotice({ text: "已复制" });
    } catch {
      report(new Error("浏览器不允许访问剪贴板"));
    }
  }
  const running = asking || !!conversation?.active_request_id;
  return (
    <CloudComputerProvider
      scope={{ userId: session.user.id, workspaceId: session.workspace_id, conversationId: conversation?.id ?? null }}>
      <WorkspaceNavigation>
        <button className="secondary new-chat" onClick={newChat}>
          <Plus size={16} />
          新建对话
        </button>
        <ConversationHistory items={conversations} selected={conversationId ?? undefined} libraries={libraries}
          onSelect={(id, message) => { saveDraft(); setSidebar(false); router.push(`/?conversation=${encodeURIComponent(id)}${message ? `&message=${encodeURIComponent(message)}` : ""}`, { scroll: false }); }}
          onRename={item => { setRenameTarget(item); setTitle(item.title); setEditing(true); setModalError(""); }}
          onDelete={item => { setDeleting(conversations.find(value => value.id === item.id) ?? null); setModalError(""); }}
          onPin={item => void pin(item)} cursor={historyCursor} onMore={() => void refreshList(historyCursor)}
          busy={historyLoading} error={historyError} retry={() => void refreshList()} />
      </WorkspaceNavigation>
      <WorkspaceHeader title={conversation?.title || "新对话"} titleContent={conversation && (!renameTarget || renameTarget.id === conversation.id) ? (
        <InlineConversationTitle title={conversation.title} busy={modalBusy} error={modalError} value={title}
          editing={editing} onStart={() => { setRenameTarget(conversation); setTitle(conversation.title); setEditing(true); setModalError(""); }}
          onChange={setTitle} onSave={() => void rename()} onCancel={() => { setEditing(false); setRenameTarget(null); }} />
      ) : undefined} />
      <div className="chat-main">
        {conversationError && <div className="workspace-request-error" role="alert">
          <CircleAlert size={16} /><span>{conversationError}</span>
          <button className="secondary" onClick={() => setReloadAttempt(value => value + 1)}><RefreshCw size={15} />重试</button>
          <button className="secondary" onClick={newChat}><Plus size={15} />新建对话</button>
        </div>}
        <section
          className="chat-messages"
          aria-label="对话内容"
          ref={scrollArea}
          onScroll={() => {
            const area = scrollArea.current;
            if (area) {
              follow.current =
                area.scrollHeight - area.scrollTop - area.clientHeight < 100;
              setLatest(!follow.current);
            }
          }}
        >
          {loading ? (
            <WorkspaceSkeleton />
          ) : !conversation?.messages.length ? (
            <Motion.div
              className="answer-empty"
              initial={{ opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={{ duration: 0.34 }}
            >
              <span className="answer-emblem">
                <img src="/icon.svg" width={32} height={32} alt="" />
              </span>
              <h2>今天想了解什么？</h2>
            </Motion.div>
          ) : (
            <div className="chat-transcript">
              {conversation.messages.map((message, index) => (
                <Fragment key={message.id}>
                <article
                  id={`chat-message-${message.id}`}
                  className={`chat-message ${message.role}${highlightedMessage === message.id ? " message-search-target" : ""}`}
                  aria-label={message.role === "user" ? "你的问题" : "知库回答"}
                >
                  <span className="message-label">
                    {message.role === "user" ? (
                      "你"
                    ) : (
                      <>
                        <img src="/icon.svg" width={16} height={16} alt="" />
                        知库
                      </>
                    )}
                    {message.refused && (
                      <span className="answer-label refused">证据不足</span>
                    )}
                  </span>
                  {message.role === "user" ? (
                    <><MessageAttachments items={message.attachments} />
                      {message.content && <p className="chat-question">{mentionRanges(message.content, storedMentions(message)).map((part, partIndex) => part.mention
                        ? <span key={`${part.mention.kind}-${part.mention.id}-${partIndex}`} className="chat-document-mention"
                          title={part.mention.kind === "knowledge_base" ? part.mention.name
                            : part.mention.kind === "resource" ? `资源文件 · ${part.mention.path}`
                            : `${part.mention.filename} · ${part.mention.knowledge_base_name ?? "知识库"}`}>{part.text}</span>
                        : <Fragment key={partIndex}>{part.text}</Fragment>)}</p>}</>
                  ) : (
                    <>
                      {message.display_blocks?.length ? processBlocks(message.display_blocks).map(block => {
                        if (block.type === "text") return <AnswerMarkdown key={block.id} message={message} text={block.text} onCitation={setPreview} />;
                        const thinking = (detail: Extract<DisplayBlock, { type: "thinking" }>) =>
                          <ConversationThinking key={detail.id} block={detail} conversationId={conversation.id}
                            messageId={message.id} messageStatus={message.status} />;
                        const callIds = new Set(block.blocks.flatMap(detail => detail.type === "activity" ? [detail.call_id] : []));
                        const activities = (message.tool_events ?? []).filter(item => callIds.has(item.call_id));
                        const firstWebCall = message.tool_events?.find(item => item.name === "web_search" || item.name === "web_fetch");
                        const sources = message.web_sources?.filter(source => source.call_ids?.length
                          ? source.call_ids.some(id => callIds.has(id)) : !!firstWebCall && callIds.has(firstWebCall.call_id)) ?? [];
                        return <ConversationActivity key={block.id} items={activities.map(activityItem)} workspaceId={session?.workspace_id}
                          finished={message.status !== "streaming" || block.blocks.at(-1)?.id !== message.display_blocks?.at(-1)?.id}
                          thinking={block.blocks.some(detail => detail.type === "thinking" && detail.status === "running")}
                          steps={block.blocks.map(detail => detail.type === "activity"
                            ? { type: "activity", id: detail.call_id }
                            : { type: "detail", id: detail.id, content: thinking(detail) })}>
                          {!!sources.length && <div className="source-list activity-sources" aria-label="网页来源">{sources.map(source => <a className="source-chip" key={source.source_id} href={source.url} target="_blank" rel="noopener noreferrer" title={`${source.url} · ${new Date(source.fetched_at).toLocaleString("zh-CN")}`}>
                            <Globe size={14} /><strong>{source.title}</strong><ChevronRight size={14} />
                          </a>)}</div>}
                        </ConversationActivity>;
                      }) : <AnswerMarkdown message={message} onCitation={setPreview} />}
                      {message.status === "streaming" && !message.display_blocks?.length && (
                        <Motion.div
                          className="thinking"
                          role="status"
                          initial={{ opacity: 0, y: 4 }}
                          animate={{ opacity: 1, y: 0 }}
                        >
                          <Loader2 size={15} className="spin" />
                          {phase || "正在生成回答…"}
                        </Motion.div>
                      )}
                      {["failed", "cancelled", "interrupted"].includes(
                        message.status,
                      ) && (
                        <div className="chat-message-error">
                          <CircleAlert size={15} />
                          <span>{message.error_detail || "已停止生成"}</span>
                          {index === conversation.messages.length - 1 && (
                            <button
                              className="text-button"
                              disabled={running || modelLoading || !!modelError || thinkingStale || !modelOptions?.providers.some(item => item.id === modelId && item.available)}
                              onClick={() => void send(message.id)}
                            >
                              <RefreshCw size={13} />
                              重试
                            </button>
                          )}
                        </div>
                      )}
                      {!!message.citations.length && (
                        <details className="sources source-disclosure">
                          <summary className="activity-toggle">
                            <ChevronRight size={14} className="activity-chevron" aria-hidden="true" />
                            参考来源<span>{message.citations.length}</span>
                          </summary>
                          <div className="source-list">
                            {message.citations.map((citation, number) => (
                              <Motion.button
                                className="source-chip"
                                key={citation.chunk_id}
                                title={`${citation.knowledge_base_name} · ${citation.filename}`}
                                onClick={() => setPreview(citation)}
                                initial={{ opacity: 0, x: -6 }}
                                animate={{ opacity: 1, x: 0 }}
                                transition={{
                                  duration: 0.22,
                                  delay: Math.min(number, 4) * 0.035,
                                }}
                                whileHover={{ x: 3 }}
                              >
                                <span>{number + 1}</span>
                                <FileText size={14} />
                                <strong>
                                  {citation.knowledge_base_name} · {citation.filename} · v{citation.version_number ?? 1}{citation.content_origin === "ai_assisted" ? " · AI 辅助生成" : ""}
                                  {citation.page_number ? ` · 第 ${citation.page_number} 页` : ""}
                                </strong>
                                <ChevronRight size={14} />
                              </Motion.button>
                            ))}
                          </div>
                        </details>
                      )}
                      {message.status !== "streaming" && (
                        <div className="answer-actions">
                          {message.content && (
                            <button
                              className="icon-button"
                              title="复制回答"
                              aria-label="复制回答"
                              onClick={() => void copy(message.content)}
                            >
                              <Copy size={15} />
                            </button>
                          )}
                          {message.status === "completed" && (
                            <AnswerFeedback
                              messageId={message.id}
                              value={message.feedback}
                              onChange={(feedback) =>
                                setConversation((current) =>
                                  current?.id === conversation.id
                                    ? {
                                        ...current,
                                        messages: current.messages.map(
                                          (item) =>
                                            item.id === message.id
                                              ? { ...item, feedback }
                                              : item,
                                        ),
                                      }
                                    : current,
                                )
                              }
                            />
                          )}
                          {(!!message.model || !!message.thinking || !!message.traces.length || !!message.tool_events?.length) && (
                            <details className="answer-details">
                              <summary>回答详情</summary>
                              <dl className="conversation-detail-metadata">
                                {message.model && <div><dt>模型</dt><dd>{message.provider_name} · {message.model}</dd></div>}
                                {message.thinking && <div><dt>思考模式</dt><dd>{message.thinking.label}</dd></div>}
                              </dl>
                              {!message.display_blocks?.length && message.tool_events?.map((item, toolIndex) => (
                                <Motion.div
                                  key={item.call_id}
                                  className="tool-event"
                                  initial={{ opacity: 0, x: -5 }}
                                  animate={{ opacity: 1, x: 0 }}
                                  transition={{ delay: Math.min(toolIndex, 4) * 0.04 }}
                                >
                                  {item.kind === "skill" ? <Puzzle size={13} /> : <Search size={13} />}
                                  <strong>{item.label}</strong>
                                  <span className="tool-status">{item.status === "completed" ? "已完成" : item.status === "error" ? "失败" : "未完成"}</span>
                                </Motion.div>
                              ))}
                              {message.traces.map((item) => (
                                <button
                                  key={item.id}
                                  className="text-button"
                                  onClick={() => setTrace(item)}
                                >
                                  <Search size={13} />
                                  {item.knowledge_base_name} · 检索记录
                                </button>
                              ))}
                            </details>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </article>
                {conversation.compactions?.filter(item => item.after_sequence === message.sequence).map(item => (
                  <div key={item.id} className="conversation-compaction" role="note">
                    <Minimize2 size={14} aria-hidden="true" /><span>上下文已压缩</span>
                  </div>
                ))}
                </Fragment>
              ))}
            </div>
          )}
          {asking &&
            !conversation?.messages.some(
              (item) => item.status === "streaming",
            ) && (
              <div className="chat-transcript">
                <Motion.div
                  className="thinking"
                  role="status"
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <Loader2 size={16} className="spin" />
                  {phase}
                </Motion.div>
              </div>
            )}
        </section>
        {latest && <div className="conversation-latest"><button type="button" onClick={() => {
          scrollArea.current?.scrollTo({ top: scrollArea.current.scrollHeight, behavior: "instant" });
          follow.current = true; setLatest(false);
        }}><ArrowDown size={14} />回到最新</button></div>}
        <div className="composer-area">
          <CloudComputerPanel key={`${session.user.id}:${session.workspace_id}:${conversation?.id ?? "new"}`} />
          {modelError && <div className="model-status form-error" role="alert">
            <span>{modelError}</span><button className="text-button" onClick={() => setModelAttempt(value => value + 1)}><RefreshCw size={14} />重试</button>
          </div>}
          {!modelLoading && !modelError && modelOptions && !modelOptions.providers.some(item => item.available) &&
            <p className="model-status" role="status">暂无可用模型，请联系站点管理员。</p>}
          {thinkingStale && <div className="model-status form-error" role="alert">
            <span>思考配置已更新，请确认当前设置。</span>
            <button type="button" className="text-button" disabled={modelLoading || !!modelError} onClick={() => setThinkingStale(false)}>
              确认设置
            </button>
          </div>}
          <form
            className={`composer${dragging ? " attachment-dragging" : ""}`}
            onDragOver={event => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }}
            onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
            onDrop={event => {
              event.preventDefault(); setDragging(false);
              if (running || loading) return;
              const error = attachments.add(Array.from(event.dataTransfer.files), !!selectedModel?.supports_images);
              if (error) report(new Error(error));
            }}
            onSubmit={(event) => {
              event.preventDefault();
              void send();
            }}
          >
            <DraftAttachments items={attachments.items} disabled={running || loading}
              onRemove={attachments.remove} onRetry={item => void attachments.retry(item)} />
            {attachments.hasImages && !selectedModel?.supports_images &&
              <p className="chat-attachment-warning" role="alert">当前模型不支持图片，请选择支持图片的模型。</p>}
            <DocumentMentionMenu key={`mentions:${session.workspace_id}:${conversation?.id ?? "draft"}`} ref={mentionMenu}
              draft={question} caret={caret} disabled={running || loading || extensionBusy}
              supportsTools={selectedModel?.supports_tools !== false}
              selected={documentMentions} input={composerInput} onActive={updateMentionState}
              onChoose={(item, start, end) => {
                const token = mentionToken(item);
                const tail = question.slice(end);
                const inserted = `${token} `;
                const value = question.slice(0, start) + inserted + tail;
                const nextCaret = start + inserted.length;
                setQuestion(value); setCaret(nextCaret);
                setDocumentMentions(current => [...pruneMentions(question, value, nextCaret, current),
                  { ...item, start, end: start + token.length }]);
                requestAnimationFrame(() => { composerInput.current?.focus(); composerInput.current?.setSelectionRange(nextCaret, nextCaret); });
              }} />
            <ExtensionSlashMenu key={`extensions:${session.workspace_id}:${conversation?.id ?? "draft"}`} ref={slashMenu}
              compactDisabled={contextLoading || modelLoading || !!contextError || !!modelError || !!conversationError || thinkingStale || !conversation || !selectedModel?.available || !contextInfo.eligible || contextInfo.status === "running"}
              onCompact={() => setCompactRequest(value => value + 1)}
              draft={question} caret={caret} input={composerInput} disabled={running || loading || extensionBusy || mentionState.open}
              supportsTools={selectedModel?.supports_tools !== false} selection={conversation
                ? { skill_ids: conversation.skill_ids, mcp_server_ids: conversation.mcp_server_ids, question_workbench: conversation.question_workbench } : draftExtensions}
              onActive={updateSlashState} onChange={changeExtensions} onInsert={(value, position) => {
                setDocumentMentions(items => pruneMentions(question, value, position, items));
                setQuestion(value); setCaret(position);
                requestAnimationFrame(() => { composerInput.current?.focus(); composerInput.current?.setSelectionRange(position, position); });
              }} />
            <div className={`composer-input-shell${documentMentions.length ? " has-document-mentions" : ""}`}>
              {!!documentMentions.length && <div className="composer-mention-overlay" aria-hidden="true">
                {mentionRanges(question, documentMentions).map((part, partIndex) => part.mention
                  ? <span key={`${part.mention.id}-${partIndex}`} className="composer-mention-link">{part.text}</span>
                  : <Fragment key={partIndex}>{part.text}</Fragment>)}
                {"\u200b"}
              </div>}
            <textarea
              ref={composerInput}
              aria-label="发送消息"
              aria-autocomplete={activeComposerMenu.open ? "list" : undefined}
              aria-controls={activeComposerMenu.open ? (mentionState.open ? "composer-document-options" : "composer-extension-options") : undefined}
              aria-activedescendant={activeComposerMenu.option}
              placeholder="输入消息，@ 引用资料，/ 命令与扩展"
              rows={2}
              value={question}
              disabled={running || loading || extensionBusy}
              onChange={(event) => { const value = event.target.value; const position = event.target.selectionStart; setQuestion(value); setCaret(position); setDocumentMentions(items => pruneMentions(question, value, position, items)); }}
              onSelect={event => setCaret(event.currentTarget.selectionStart)}
              onScroll={event => { const overlay = event.currentTarget.parentElement?.querySelector<HTMLElement>(".composer-mention-overlay"); if (overlay) overlay.scrollTop = event.currentTarget.scrollTop; }}
              onPaste={event => {
                const files = Array.from(event.clipboardData?.files ?? []);
                if (!files.length) return;
                event.preventDefault();
                const error = attachments.add(files, !!selectedModel?.supports_images);
                if (error) report(new Error(error));
              }}
              onKeyDown={(event) => {
                if (mentionMenu.current?.keyDown(event) || slashMenu.current?.keyDown(event)) return;
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            </div>
            <div className="composer-footer">
              <AttachmentButton disabled={running || loading || modelLoading || !selectedModel?.available}
                select={item => attachments.selectResource(item, !!selectedModel?.supports_images)}
                add={files => { const error = attachments.add(files, !!selectedModel?.supports_images); if (error) report(new Error(error)); }} />
              <RequestModelPicker models={modelOptions?.providers ?? []} modelId={modelId} thinkingId={thinkingId}
                loading={modelLoading} disabled={running || loading || modelLoading || !!modelError}
                onModelChange={id => { setModelId(id); setThinkingStale(false); }} onThinkingChange={setThinkingId} />
              <ExtensionPicker disabled={running || loading || extensionBusy} supportsTools={selectedModel?.supports_tools !== false}
                selection={conversation ? { skill_ids: conversation.skill_ids, mcp_server_ids: conversation.mcp_server_ids, question_workbench: conversation.question_workbench } : draftExtensions}
                onChange={changeExtensions} />
              <Tooltip content={webFailed ? "联网状态加载失败，点击重试" : webAvailable ? `${webEnabled ? "自动联网" : "联网已关闭"} · 按有效调用计费` : "联网搜索暂不可用"}>
                <button type="button" className="icon-button web-search-toggle" aria-label={webFailed ? "重试联网状态" : "自动联网"} aria-pressed={webEnabled && webAvailable}
                  disabled={running || loading || selectedModel?.supports_tools === false || (!webAvailable && !webFailed)} onClick={() => webFailed ? setWebAttempt(value => value + 1) : setWebEnabled(value => !value)}><Globe size={17} /></button>
              </Tooltip>
              <ConversationContextControl state={contextInfo} loading={contextLoading || modelLoading || loading}
                compactRequest={compactRequest}
                modelSelected={!!selectedModel} loadError={modelError || contextError || conversationError}
                disabled={running || loading || modelLoading || thinkingStale || !conversation || !selectedModel?.available}
                onRetry={() => {
                  if (modelError) setModelAttempt(value => value + 1);
                  else if (conversationError) setReloadAttempt(value => value + 1);
                  else setContextAttempt(value => value + 1);
                }} onCompact={() => void compact()} />
              {running ? (
                <button
                  key="stop"
                  className="send-button"
                  type="button"
                  title={contextInfo.status === "running" ? "停止整理" : "停止回答"}
                  aria-label={contextInfo.status === "running" ? "停止整理" : "停止回答"}
                  onClick={(event) => {
                    event.preventDefault();
                    void stop();
                  }}
                >
                  <Square size={14} />
                </button>
              ) : (
                <button
                  key="send"
                  className="send-button"
                  title="发送问题"
                  aria-label="发送问题"
                  disabled={
                    (!question.trim() && !attachments.ready.length) || attachments.blocked ||
                    (attachments.hasImages && !selectedModel?.supports_images) ||
                    loading || extensionBusy || !!conversationError || modelLoading || !!modelError || thinkingStale ||
                    !modelOptions?.providers.some(item => item.id === modelId && item.available)
                  }
                  type="submit"
                >
                  <ArrowUp size={19} />
                </button>
              )}
            </div>
          </form>
          <div className="composer-meta">
            <span>AI 回答仅供参考，请核对来源</span>
          </div>
        </div>
      </div>
      {notice && (
        <div
          className={`toast ${notice.error ? "error" : ""}`}
          role={notice.error ? "alert" : "status"}
        >
          <CircleAlert size={17} />
          <span>{notice.text}</span>
          <button
            className="icon-button"
            aria-label="关闭通知"
            onClick={() => setNotice(null)}
          >
            <X size={15} />
          </button>
        </div>
      )}
      {preview && (
        <Modal title="来源详情" wide onClose={() => setPreview(null)}>
          <div className="preview-heading">
            <span className="file-symbol">
              <FileText size={22} />
            </span>
            <div>
              <h3>{preview.filename}</h3>
              <p>
                {preview.knowledge_base_name} · v{preview.version_number ?? 1} ·{" "}
                {preview.page_number ? `第 ${preview.page_number} 页 · ` : ""}
                {preview.heading_path ? `${preview.heading_path} · ` : ""}字符{" "}
                {preview.char_start}–{preview.char_end}
              </p>
            </div>
          </div>
          <div className="preview-text">{preview.text}</div>
        </Modal>
      )}
      {trace && (
        <Modal title="检索记录" wide onClose={() => setTrace(null)}>
          <TraceInspector
            traceId={trace.id}
            knowledgeBaseId={trace.knowledge_base_id}
          />
        </Modal>
      )}
      {editing && renameTarget?.id !== conversation?.id && (
        <Modal
          title="重命名对话"
          busy={modalBusy}
          onClose={() => setEditing(false)}
        >
          <form className="modal-form" onSubmit={(event) => void rename(event)}>
            <label>
              名称
              <input
                autoFocus
                required
                maxLength={200}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            {modalError && (
              <p className="form-error" role="alert">
                {modalError}
              </p>
            )}
            <div className="modal-footer">
              <button
                className="secondary"
                type="button"
                disabled={modalBusy}
                onClick={() => setEditing(false)}
              >
                取消
              </button>
              <button
                className="primary"
                disabled={modalBusy || !title.trim()}
                type="submit"
              >
                保存
              </button>
            </div>
          </form>
        </Modal>
      )}
      {deleting && (
        <Modal
          title="删除对话"
          busy={modalBusy}
          onClose={() => setDeleting(null)}
        >
          <div className="modal-form">
            <p className="delete-conversation-name">{deleting.title}</p>
            {modalError && (
              <p className="form-error" role="alert">
                {modalError}
              </p>
            )}
            <div className="modal-footer">
              <button
                className="secondary"
                disabled={modalBusy}
                onClick={() => setDeleting(null)}
              >
                取消
              </button>
              <button
                className="primary delete-confirm"
                disabled={modalBusy}
                onClick={() => void remove()}
              >
                <Trash2 size={15} />
                删除
              </button>
            </div>
          </div>
        </Modal>
      )}
    </CloudComputerProvider>
  );
}
