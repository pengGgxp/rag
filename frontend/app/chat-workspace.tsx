"use client";

import ReactMarkdown from "react-markdown";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Copy,
  Download,
  FileText,
  Layers3,
  Loader2,
  Menu,
  MessageSquare,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { API_URL, request, streamRequest } from "./api";
import { Modal } from "./modal";
import { AccountButton } from "./auth";
import { AnswerFeedback, type Feedback } from "./answer-feedback";
import { TraceInspector } from "./retrieval-panel";
import "./chat.css";

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
  knowledge_base_name: string;
  knowledge_base_id: string;
};
type Trace = {
  id: string;
  knowledge_base_name: string;
  knowledge_base_id: string;
};
type Message = {
  id: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  status: string;
  request_id?: string | null;
  citations: Citation[];
  traces: Trace[];
  refused: boolean;
  error_detail?: string | null;
  feedback?: Feedback | null;
};
type Conversation = {
  id: string;
  title: string;
  knowledge_bases: { id: string; name: string }[];
  active_request_id: string | null;
  updated_at: string;
  messages: Message[];
};
type Generation = {
  controller: AbortController;
  conversationId: string;
  requestId: string;
  epoch: number;
};

function remember(id?: string) {
  const url = new URL(window.location.href);
  url.search = id
    ? new URLSearchParams({ conversation: id }).toString()
    : "chat=1";
  window.history.replaceState(null, "", url);
}
function upsert(messages: Message[], message: Message) {
  return [...messages.filter((item) => item.id !== message.id), message].sort(
    (a, b) => a.sequence - b.sequence,
  );
}
function AnswerMarkdown({
  message,
  onCitation,
}: {
  message: Message;
  onCitation: (item: Citation) => void;
}) {
  let markdown = message.content;
  message.citations.forEach((citation, index) => {
    markdown = markdown
      .split(`[${citation.chunk_id}]`)
      .join(`[${index + 1}](#source-${citation.chunk_id})`);
  });
  return (
    <div className="answer-text">
      <ReactMarkdown
        skipHtml
        components={{
          img: ({ alt }) => <span>{alt}</span>,
          a: ({ href, children }) => {
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
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

export function ChatWorkspace({
  libraries,
  initialLibraryId,
  onKnowledge,
}: {
  libraries: Library[];
  initialLibraryId: string;
  onKnowledge: () => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [scope, setScope] = useState<string[]>(
    initialLibraryId ? [initialLibraryId] : [],
  );
  const [question, setQuestion] = useState("");
  const [search, setSearch] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [loading, setLoading] = useState(false);
  const [asking, setAsking] = useState(false);
  const [phase, setPhase] = useState("");
  const [sidebar, setSidebar] = useState(false);
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
  viewRef.current = conversation;
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
    const controller = new AbortController();
    void refreshList();
    const id = new URLSearchParams(window.location.search).get("conversation");
    if (id) {
      setLoading(true);
      request<Conversation>(`/conversations/${id}`, {
        signal: controller.signal,
      })
        .then((item) => {
          if (!controller.signal.aborted) setConversation(item);
        })
        .catch((error) => {
          if (!controller.signal.aborted) {
            report(error);
            remember();
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    } else remember();
    return () => {
      controller.abort();
      epoch.current++;
      void stopGeneration();
    };
  }, [refreshList, report, stopGeneration]);
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

  async function select(item: Conversation) {
    const currentEpoch = ++epoch.current;
    sending.current = false;
    setAsking(false);
    setLoading(true);
    setSidebar(false);
    setQuestion("");
    setPreview(null);
    setTrace(null);
    await stopGeneration();
    generation.current = null;
    try {
      const detail = await request<Conversation>(`/conversations/${item.id}`);
      if (epoch.current === currentEpoch) {
        follow.current = true;
        setConversation(detail);
        remember(detail.id);
      }
    } catch (error) {
      if (epoch.current === currentEpoch) report(error);
    } finally {
      if (epoch.current === currentEpoch) setLoading(false);
    }
  }
  function newChat() {
    ++epoch.current;
    void stopGeneration();
    generation.current = null;
    sending.current = false;
    setConversation(null);
    if (conversation)
      setScope(conversation.knowledge_bases.map((item) => item.id));
    setLoading(false);
    setQuestion("");
    setAsking(false);
    setSidebar(false);
    setPreview(null);
    setTrace(null);
    follow.current = true;
    remember();
  }
  function leave() {
    void stopGeneration();
    const url = new URL(window.location.href);
    url.search = "";
    window.history.replaceState(null, "", url);
    onKnowledge();
  }
  async function reloadCurrent(id: string, currentEpoch: number) {
    try {
      const saved = await request<Conversation>(`/conversations/${id}`);
      if (epoch.current === currentEpoch) setConversation(saved);
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
      loading ||
      (!retryId && !question.trim()) ||
      !(conversation ? conversation.knowledge_bases.length : scope.length) ||
      conversation?.active_request_id
    )
      return;
    sending.current = true;
    setAsking(true);
    setPhase("正在检索资料…");
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
          body: JSON.stringify({ knowledge_base_ids: scope }),
          signal: controller.signal,
        });
        if (epoch.current !== currentEpoch) return;
        setConversation(current);
        remember(current.id);
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
                    traces: [],
                  }
                : item,
            )
          : [...current.messages, pendingUser],
      });
      await streamRequest(
        `/conversations/${current.id}/messages`,
        {
          request_id: active.requestId,
          ...(retryId ? { retry_message_id: retryId } : { query }),
        },
        controller.signal,
        (name, data) => {
          if (epoch.current !== currentEpoch || controller.signal.aborted)
            return;
          if (name === "phase")
            setPhase(
              (data as { phase: string }).phase === "retrieving"
                ? "正在检索资料…"
                : "正在生成回答…",
            );
          if (name === "message") received = true;
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
              if (name === "delta")
                return {
                  ...message,
                  content: message.content + (data as { text: string }).text,
                };
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
        report(error);
        if (!received && !retryId) setQuestion(query);
        if (active) await stopGeneration();
      }
    } finally {
      if (epoch.current === currentEpoch) {
        if (current) await reloadCurrent(current.id, currentEpoch);
        generation.current = null;
        sending.current = false;
        setAsking(false);
        setPhase("");
      }
      void refreshList();
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
    <div className="chat-shell">
      {sidebar && (
        <button
          className="sidebar-backdrop"
          aria-label="收起对话列表"
          onClick={() => setSidebar(false)}
        />
      )}
      <aside
        className={`chat-sidebar ${sidebar ? "is-open" : ""}`}
        aria-label="对话导航"
      >
        <button className="brand" onClick={leave}>
          <span className="brand-mark">
            <Layers3 size={23} />
          </span>
          <span>知库</span>
        </button>
        <nav className="primary-navigation" aria-label="产品导航">
          <button onClick={leave}>
            <BookOpen size={18} />
            知识库
          </button>
          <button
            className="selected"
            aria-current="page"
            onClick={() => setSidebar(false)}
          >
            <MessageSquare size={18} />
            知识问答
          </button>
          <button
            className="icon-button mobile-only"
            title="关闭导航"
            aria-label="关闭导航"
            onClick={() => setSidebar(false)}
          >
            <X size={18} />
          </button>
        </nav>
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
        <div className="sidebar-footer">
          <AccountButton />
        </div>
      </aside>
      <main className="chat-main">
        <header className="chat-header">
          <button
            className="icon-button mobile-only"
            title="打开导航"
            aria-label="打开导航"
            onClick={() => setSidebar(true)}
          >
            <Menu size={19} />
          </button>
          <h1>{conversation?.title || "新对话"}</h1>
          {conversation && (
            <div className="chat-header-actions">
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
            </div>
          )}
        </header>
        <div className="chat-scope">
          <details className="scope-menu">
            <summary>
              <BookOpen size={15} />
              <span>
                {selectedNames.length ? selectedNames.join("、") : "选择知识库"}
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
                            : ids.length < 10
                              ? [...ids, item.id]
                              : ids,
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
            if (area)
              follow.current =
                area.scrollHeight - area.scrollTop - area.clientHeight < 100;
          }}
        >
          {loading ? (
            <div className="chat-loading" role="status">
              <Loader2 size={21} className="spin" />
              正在加载对话…
            </div>
          ) : !conversation?.messages.length ? (
            <div className="answer-empty">
              <span className="answer-emblem">
                <Layers3 size={32} strokeWidth={1.4} />
              </span>
              <h2>今天想了解什么？</h2>
            </div>
          ) : (
            <div className="chat-transcript">
              {conversation.messages.map((message, index) => (
                <article
                  className={`chat-message ${message.role}`}
                  key={message.id}
                  aria-label={message.role === "user" ? "你的问题" : "知库回答"}
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
                    <p className="chat-question">{message.content}</p>
                  ) : (
                    <>
                      <AnswerMarkdown
                        message={message}
                        onCitation={setPreview}
                      />
                      {message.status === "streaming" && (
                        <div className="thinking" role="status">
                          <Loader2 size={15} className="spin" />
                          {phase || "正在生成回答…"}
                        </div>
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
                              disabled={running}
                              onClick={() => void send(message.id)}
                            >
                              <RefreshCw size={13} />
                              重试
                            </button>
                          )}
                        </div>
                      )}
                      {!!message.citations.length && (
                        <div className="sources">
                          <div className="sources-heading">
                            参考来源<span>{message.citations.length}</span>
                          </div>
                          <div className="source-list">
                            {message.citations.map((citation, number) => (
                              <button
                                className="source-chip"
                                key={citation.chunk_id}
                                title={`${citation.knowledge_base_name} · ${citation.filename}`}
                                onClick={() => setPreview(citation)}
                              >
                                <span>{number + 1}</span>
                                <FileText size={14} />
                                <strong>
                                  {conversation.knowledge_bases.length > 1
                                    ? `${citation.knowledge_base_name} · `
                                    : ""}
                                  {citation.filename}
                                </strong>
                                <ChevronRight size={14} />
                              </button>
                            ))}
                          </div>
                        </div>
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
                          {!!message.traces.length && (
                            <details className="answer-details">
                              <summary>回答详情</summary>
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
              ))}
            </div>
          )}
          {asking &&
            !conversation?.messages.some(
              (item) => item.status === "streaming",
            ) && (
              <div className="chat-transcript">
                <div className="thinking" role="status">
                  <Loader2 size={16} className="spin" />
                  {phase}
                </div>
              </div>
            )}
        </section>
        <div className="composer-area">
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void send();
            }}
          >
            <textarea
              aria-label="向知识库提问"
              placeholder={
                selectedNames.length ? "向知识库提问…" : "选择知识库后提问…"
              }
              rows={2}
              maxLength={4000}
              value={question}
              disabled={running || loading}
              onChange={(event) => setQuestion(event.target.value)}
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
              <span>
                <BookOpen size={14} />
                {selectedNames.length} 个知识库
              </span>
              {running ? (
                <button
                  key="stop"
                  className="send-button"
                  type="button"
                  title="停止回答"
                  aria-label="停止回答"
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
                    !question.trim() || !selectedNames.length || loading
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
      </main>
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
                {preview.knowledge_base_name} ·{" "}
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
    </div>
  );
}
