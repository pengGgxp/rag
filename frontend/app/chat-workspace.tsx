"use client";

import ReactMarkdown from "react-markdown";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  ArrowDown,
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Copy,
  Download,
  FileText,
  Globe,
  Layers3,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
  Puzzle,
  RefreshCw,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { API_URL, ApiError, request, streamRequest } from "./api";
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
import { useRouter } from "next/navigation";
import { RequestModelPicker } from "./request-model-picker";
import { ExtensionPicker } from "./extensions/extension-picker";
import type { ExtensionSelection } from "./extensions/extension-data";
import { ConversationActivity } from "./conversation-activity";
import { ConversationThinking } from "./conversation-thinking";
import { citationMarkers } from "./citation-markers";
import { Tooltip } from "../components/ui/tooltip";
import { ConversationContextControl, type ConversationContextState } from "./conversation-context-control";
import { activityItem, appendActivity, appendText, updateThinking, type DisplayBlock, type ToolEvent } from "./conversation-display";
import "./conversation-experience.css";
import { AttachmentButton, DraftAttachments, MessageAttachments, useChatAttachments, type ChatAttachment } from "./chat-attachments";

type Library = {
  id: string;
  name: string;
  description?: string | null;
  document_count: number;
};
type Citation = {
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
};
const emptyContext: ConversationContextState = {
  estimatedInputTokens: null, capacity: null, outputReserve: null, eligible: false, status: "idle",
};
function contextState(value: ContextResponse): ConversationContextState {
  return { estimatedInputTokens: value.estimated_input_tokens, capacity: value.capacity,
    outputReserve: value.output_reserve, eligible: value.eligible, status: value.status };
}
type Conversation = {
  id: string;
  title: string;
  knowledge_bases: { id: string; name: string }[];
  active_request_id: string | null;
  updated_at: string;
  messages: Message[];
  skill_ids: string[];
  mcp_server_ids: string[];
};
type Generation = {
  controller: AbortController;
  conversationId: string;
  requestId: string;
  epoch: number;
};

function upsert(messages: Message[], message: Message) {
  return [...messages.filter((item) => item.id !== message.id), message].sort(
    (a, b) => a.sequence - b.sequence,
  );
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
  return (
    <div className="answer-text">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[citationMarkers([...message.citations, ...(message.web_sources ?? [])])]}
        components={{
          img: ({ alt }) => <span>{alt}</span>,
          a: ({ href, children }) => {
            const web = message.web_sources?.find(item => href === `#source-${item.source_id}`);
            if (web) return <a className="inline-citation" href={web.url} target="_blank" rel="noopener noreferrer" title={`查看网页：${web.title}`}>{children}</a>;
            const citation = message.citations.find(
              (item) => href === `#source-${item.chunk_id}`,
            );
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
  initialLibraryId,
  conversationId,
}: {
  libraries: Library[];
  initialLibraryId: string;
  conversationId: string | null;
}) {
  const router = useRouter();
  const { session } = useAuth();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [draftExtensions, setDraftExtensions] = useState<ExtensionSelection | null>(null);
  const [scope, setScope] = useState<string[]>(
    initialLibraryId ? [initialLibraryId] : [],
  );
  const [question, setQuestion] = useState("");
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
  const [search, setSearch] = useState("");
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
  const [latest, setLatest] = useState(false);
  const setSidebar = useUiStore((state) => state.setSidebarOpen);
  const [notice, setNotice] = useState<{
    text: string;
    error?: boolean;
  } | null>(null);
  const [preview, setPreview] = useState<Citation | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [modalBusy, setModalBusy] = useState(false);
  const [modalError, setModalError] = useState("");
  const generation = useRef<Generation | null>(null);
  const epoch = useRef(0);
  const viewRef = useRef<Conversation | null>(null);
  const sending = useRef(false);
  const scrollArea = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const draftScopeInitialized = useRef(!!initialLibraryId);
  const restoredModelFor = useRef<string | null>(null);
  viewRef.current = conversation;
  const selectedModel = modelOptions?.providers.find(item => item.id === modelId);
  const thinking = selectedModel?.thinking;
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
        if (!controller.signal.aborted) setContextInfo(contextState(value));
      }).catch(error => {
        if (!controller.signal.aborted) setContextError(
          error instanceof Error ? error.message : "上下文状态加载失败");
      }).finally(() => { if (!controller.signal.aborted) setContextLoading(false); });
    return () => controller.abort();
  }, [conversation?.id, modelId, selectedModel, thinkingId, thinkingVersion, contextAttempt, webEnabled, webAvailable]);
  const selectedNames = conversation
    ? conversation.knowledge_bases.map((item) => item.name)
    : libraries
        .filter((item) => scope.includes(item.id))
        .map((item) => item.name);

  const report = useCallback((error: unknown) => {
    setNotice({
      text: error instanceof Error ? error.message : "请求失败，请重试",
      error: true,
    });
  }, []);
  const refreshList = useCallback(async () => {
    setHistoryError("");
    try {
      setConversations(await request<Conversation[]>("/conversations"));
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "对话加载失败");
    } finally {
      setHistoryLoading(false);
    }
  }, []);
  const stopGeneration = useCallback(async () => {
    const active = generation.current;
    if (!active) return;
    active.controller.abort();
    await request(`/conversations/${active.conversationId}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: active.requestId }),
    }).catch(() => {});
  }, []);
  useEffect(() => {
    void refreshList();
    return () => {
      epoch.current++;
      void stopGeneration();
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
  useEffect(() => {
    // Creating a conversation replaces the URL while its stream is already active.
    if (conversationId && viewRef.current?.id === conversationId) return;
    const controller = new AbortController();
    const currentEpoch = ++epoch.current;
    void stopGeneration();
    generation.current = null;
    sending.current = false;
    setAsking(false);
    setQuestion("");
    attachments.reset();
    setPreview(null);
    setTrace(null);
    if (viewRef.current) {
      setScope(viewRef.current.knowledge_bases.map(item => item.id));
    }
    setConversation(null);
    setConversationError("");
    follow.current = true;
    setLoading(!!conversationId);
    if (conversationId) void request<Conversation>(`/conversations/${encodeURIComponent(conversationId)}`, { signal: controller.signal })
      .then(item => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) setConversation(item);
      })
      .catch(error => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) {
          setConversationError(error instanceof Error ? error.message : "对话加载失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch.current === currentEpoch) setLoading(false);
      });
    return () => controller.abort();
  }, [conversationId, reloadAttempt, stopGeneration]);
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
    if (!draftScopeInitialized.current && libraries.length) {
      setScope([initialLibraryId || libraries[0].id]);
      draftScopeInitialized.current = true;
    }
  }, [libraries, initialLibraryId]);
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
    const timer = setInterval(() => {
      request<Conversation>(`/conversations/${id}`)
        .then((item) => {
          if (viewRef.current?.id === id && !generation.current)
            setConversation(item);
        })
        .catch(() => {});
    }, 1500);
    return () => clearInterval(timer);
  }, [conversation?.id, conversation?.active_request_id, asking]);

  function select(item: Conversation) {
    setSidebar(false);
    if (item.id !== conversationId)
      router.push(`/?conversation=${encodeURIComponent(item.id)}`, { scroll: false });
  }
  function newChat() {
    setDraftExtensions(null);
    ++epoch.current;
    void stopGeneration();
    generation.current = null;
    sending.current = false;
    attachments.reset();
    setConversation(null);
    setConversationError("");
    if (conversation)
      setScope(conversation.knowledge_bases.map((item) => item.id));
    setLoading(false);
    setQuestion("");
    setAsking(false);
    setSidebar(false);
    setPreview(null);
    setTrace(null);
    follow.current = true;
    if (conversationId) router.push("/?chat=1", { scroll: false });
  }
  async function reloadCurrent(id: string, currentEpoch: number) {
    try {
      const saved = await request<Conversation>(`/conversations/${id}`);
      if (epoch.current === currentEpoch) setConversation(saved);
      return saved;
    } catch (error) {
      if (epoch.current === currentEpoch) report(error);
    }
  }
  async function stop() {
    if (generation.current) {
      const active = generation.current;
      await stopGeneration();
      await reloadCurrent(active.conversationId, active.epoch);
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
  async function send(retryId?: string) {
    if (
      sending.current ||
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
    const controller = new AbortController();
    let current = conversation;
    let active: Generation | null = null;
    let received = false;
    try {
      if (!current) {
        current = await request<Conversation>("/conversations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ knowledge_base_ids: scope, ...(draftExtensions ?? {}) }),
          signal: controller.signal,
        });
        if (epoch.current !== currentEpoch) return;
        setConversation(current);
        viewRef.current = current;
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
      setQuestion("");
      follow.current = true;
      const pendingUser: Message = {
        id: `pending-${active.requestId}`,
        sequence: (current.messages.at(-1)?.sequence ?? -1) + 1,
        role: "user",
        content: query,
        attachments: attachments.ready,
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
          ...(thinkingId ? { thinking_option_id: thinkingId } : {}),
          ...(retryId ? { retry_message_id: retryId } : {
            ...(query ? { query } : {}), attachment_ids: attachments.ready.map(item => item.id),
          }),
        },
        controller.signal,
        (name, data) => {
          if (epoch.current !== currentEpoch || controller.signal.aborted)
            return;
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
          if (name === "compaction") {
            const status = (data as ToolEvent).status;
            setContextInfo(value => ({ ...value, status: status === "running" ? "running" : status === "completed" ? "completed" : "failed" }));
            setPhase(status === "running" ? "正在整理早期上下文…" : "正在继续回答…");
          }
          if (name === "message" || name === "done") {
            received = true;
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
        if (!received && !retryId) setQuestion(query);
        if (active) await stopGeneration();
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
        if (name === "context") setContextInfo(contextState(data as ContextResponse));
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
  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!conversation) return;
    setModalBusy(true);
    setModalError("");
    try {
      const updated = await request<Conversation>(
        `/conversations/${conversation.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.trim() }),
        },
      );
      setConversation((item) =>
        item ? { ...item, title: updated.title } : item,
      );
      setEditing(false);
      void refreshList();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "重命名失败");
    } finally {
      setModalBusy(false);
    }
  }
  async function remove() {
    if (!deleting) return;
    setModalBusy(true);
    setModalError("");
    try {
      await request(`/conversations/${deleting.id}`, { method: "DELETE" });
      if (conversation?.id === deleting.id) newChat();
      setDeleting(null);
      void refreshList();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "删除失败");
    } finally {
      setModalBusy(false);
    }
  }
  async function download() {
    if (!conversation) return;
    try {
      const response = await fetch(
        `${API_URL}/conversations/${conversation.id}/export`,
        { credentials: "include" },
      );
      if (!response.ok) throw new Error("导出失败，请重试");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${conversation.title.replace(/[<>:"/\\|?*]/g, "_") || "对话"}.md`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      report(error);
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
  const visibleHistory = conversations.filter((item) =>
    item.title.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <WorkspaceNavigation>
        <button className="secondary new-chat" onClick={newChat}>
          <Plus size={16} />
          新建对话
        </button>
        <label className="search-field history-search">
          <Search size={15} />
          <input
            aria-label="搜索对话"
            placeholder="搜索对话"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <div className="history-heading">历史对话</div>
        <nav className="conversation-list" aria-label="历史对话">
          {visibleHistory.map((item) => (
            <div
              className={`conversation-item ${conversation?.id === item.id ? "selected" : ""}`}
              key={item.id}
            >
              <button
                onClick={() => void select(item)}
                aria-current={conversation?.id === item.id ? "page" : undefined}
                title={item.title}
              >
                <MessageSquare size={15} />
                <span>{item.title}</span>
              </button>
              <button
                className="icon-button"
                title="删除对话"
                aria-label={`删除对话 ${item.title}`}
                onClick={() => {
                  setDeleting(item);
                  setModalError("");
                }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {historyLoading && <p className="sidebar-empty">正在加载…</p>}
          {historyError && (
            <div className="history-error" role="alert">
              {historyError}
              <button
                className="icon-button"
                title="重新加载对话"
                aria-label="重新加载对话"
                onClick={() => void refreshList()}
              >
                <RefreshCw size={15} />
              </button>
            </div>
          )}
          {!historyLoading && !historyError && !visibleHistory.length && (
            <p className="sidebar-empty">
              {search ? "没有匹配的对话" : "暂无对话"}
            </p>
          )}
        </nav>
      </WorkspaceNavigation>
        <WorkspaceHeader title={conversation?.title || conversations.find(item => item.id === conversationId)?.title || (conversationId ? "知识问答" : "新对话")} actions={conversation && (
            <>
              <button
                className="icon-button"
                title="重命名对话"
                aria-label="重命名对话"
                onClick={() => {
                  setTitle(conversation.title);
                  setEditing(true);
                  setModalError("");
                }}
              >
                <Pencil size={16} />
              </button>
              <button
                className="icon-button"
                title="导出 Markdown"
                aria-label="导出对话"
                onClick={() => void download()}
              >
                <Download size={17} />
              </button>
            </>
          )} />
      <div className="chat-main">
        {conversationError && <div className="workspace-request-error" role="alert">
          <CircleAlert size={16} /><span>{conversationError}</span>
          <button className="secondary" onClick={() => setReloadAttempt(value => value + 1)}><RefreshCw size={15} />重试</button>
          <button className="secondary" onClick={newChat}><Plus size={15} />新建对话</button>
        </div>}
        <div className="chat-scope">
          <details className="scope-menu">
            <summary>
              <BookOpen size={15} />
              <span>
                {selectedNames.length ? selectedNames.join("、") : "知识库（可选）"}
              </span>
              <ChevronDown size={14} />
            </summary>
            <div className="scope-options">
              <fieldset disabled={!!conversation || asking}>
                <legend>知识库</legend>
                {libraries.map((item) => (
                  <label
                    key={item.id}
                    title={conversation ? "会话资料范围已固定" : item.name}
                  >
                    <input
                      type="checkbox"
                      checked={
                        conversation
                          ? conversation.knowledge_bases.some(
                              (library) => library.id === item.id,
                            )
                          : scope.includes(item.id)
                      }
                      onChange={() =>
                        setScope((ids) =>
                          ids.includes(item.id)
                            ? ids.filter((id) => id !== item.id)
                            : [...ids, item.id],
                        )
                      }
                    />
                    <span>{item.name}</span>
                    <small>{item.document_count}</small>
                  </label>
                ))}
              </fieldset>
            </div>
          </details>
        </div>
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
                <Layers3 size={32} strokeWidth={1.4} />
              </span>
              <h2>今天想了解什么？</h2>
            </Motion.div>
          ) : (
            <div className="chat-transcript">
              {conversation.messages.map((message, index) => (
                <Motion.article
                  className={`chat-message ${message.role}`}
                  key={message.id}
                  aria-label={message.role === "user" ? "你的问题" : "知库回答"}
                  initial={{ opacity: 0, y: message.role === "user" ? 8 : 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
                >
                  <span className="message-label">
                    {message.role === "user" ? (
                      "你"
                    ) : (
                      <>
                        <Layers3 size={16} />
                        知库
                      </>
                    )}
                    {message.refused && (
                      <span className="answer-label refused">证据不足</span>
                    )}
                  </span>
                  {message.role === "user" ? (
                    <><MessageAttachments items={message.attachments} />
                      {message.content && <p className="chat-question">{message.content}</p>}</>
                  ) : (
                    <>
                      {message.display_blocks?.length ? message.display_blocks.map(block => {
                        if (block.type === "text") return <AnswerMarkdown key={block.id} message={message} text={block.text} onCitation={setPreview} />;
                        if (block.type === "thinking") return <ConversationThinking key={`${message.request_id}-${block.id}`} block={block}
                          conversationId={conversation.id} messageId={message.id} messageStatus={message.status} />;
                        const activity = message.tool_events?.find(item => item.call_id === block.call_id);
                        const firstWebCall = message.tool_events?.find(item => item.name === "web_search" || item.name === "web_fetch");
                        const sources = message.web_sources?.filter(source => source.call_ids?.length
                          ? source.call_ids.includes(block.call_id) : block.call_id === firstWebCall?.call_id) ?? [];
                        return activity ? <ConversationActivity key={block.id} items={[activityItem(activity)]}
                          finished={!["waiting", "running"].includes(activity.status)}>
                          {!!sources.length && <div className="source-list activity-sources" aria-label="网页来源">{sources.map(source => <a className="source-chip" key={source.source_id} href={source.url} target="_blank" rel="noopener noreferrer" title={`${source.url} · ${new Date(source.fetched_at).toLocaleString("zh-CN")}`}>
                            <Globe size={14} /><strong>{source.title}</strong><ChevronRight size={14} />
                          </a>)}</div>}
                        </ConversationActivity> : null;
                      }) : <AnswerMarkdown message={message} onCitation={setPreview} />}
                      {message.status === "streaming" && !message.display_blocks?.some(
                        block => block.type === "thinking" && block.status === "running",
                      ) && (
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
                                  {conversation.knowledge_bases.length > 1
                                    ? `${citation.knowledge_base_name} · `
                                    : ""}
                                  {citation.filename} · v{citation.version_number ?? 1}
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
                </Motion.article>
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
            <textarea
              aria-label="向知识库提问"
              placeholder={
                selectedNames.length ? "向知识库提问…" : "发送消息…"
              }
              rows={2}
              value={question}
              disabled={running || loading}
              onChange={(event) => setQuestion(event.target.value)}
              onPaste={event => {
                const files = Array.from(event.clipboardData?.files ?? []);
                if (!files.length) return;
                event.preventDefault();
                const error = attachments.add(files, !!selectedModel?.supports_images);
                if (error) report(new Error(error));
              }}
              onKeyDown={(event) => {
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
            <div className="composer-footer">
              <AttachmentButton disabled={running || loading || modelLoading || !selectedModel?.available}
                add={files => { const error = attachments.add(files, !!selectedModel?.supports_images); if (error) report(new Error(error)); }} />
              <RequestModelPicker models={modelOptions?.providers ?? []} modelId={modelId} thinkingId={thinkingId}
                loading={modelLoading} disabled={running || loading || modelLoading || !!modelError}
                onModelChange={id => { setModelId(id); setThinkingStale(false); }} onThinkingChange={setThinkingId} />
              <ExtensionPicker disabled={running || loading} supportsTools={selectedModel?.supports_tools !== false}
                selection={conversation ? { skill_ids: conversation.skill_ids, mcp_server_ids: conversation.mcp_server_ids } : draftExtensions}
                onChange={async value => {
                  if (!conversation) { setDraftExtensions(value); return; }
                  const id = conversation.id;
                  const updated = await request<Conversation>(`/conversations/${id}/extensions`, {
                    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
                  });
                  setConversation(current => current?.id === id ? { ...current, skill_ids: updated.skill_ids, mcp_server_ids: updated.mcp_server_ids } : current);
                }} />
              <Tooltip content={webFailed ? "联网状态加载失败，点击重试" : webAvailable ? `${webEnabled ? "自动联网" : "联网已关闭"} · 按有效调用计费` : "联网搜索暂不可用"}>
                <button type="button" className="icon-button web-search-toggle" aria-label={webFailed ? "重试联网状态" : "自动联网"} aria-pressed={webEnabled && webAvailable}
                  disabled={running || loading || selectedModel?.supports_tools === false || (!webAvailable && !webFailed)} onClick={() => webFailed ? setWebAttempt(value => value + 1) : setWebEnabled(value => !value)}><Globe size={17} /></button>
              </Tooltip>
              <ConversationContextControl state={contextInfo} loading={contextLoading || modelLoading || loading}
                modelSelected={!!selectedModel} loadError={modelError || contextError || conversationError}
                disabled={running || loading || modelLoading || thinkingStale || !conversation || !selectedModel?.available}
                onRetry={() => {
                  if (modelError) setModelAttempt(value => value + 1);
                  else if (conversationError) setReloadAttempt(value => value + 1);
                  else setContextAttempt(value => value + 1);
                }} onCompact={() => void compact()} />
              <span className="composer-library-count">
                <BookOpen size={14} />
                {selectedNames.length} 个知识库
              </span>
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
                    loading || !!conversationError || modelLoading || !!modelError || thinkingStale ||
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
      {editing && (
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
    </>
  );
}
