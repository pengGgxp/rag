"use client";

import ReactMarkdown from "react-markdown";
import { request } from "./api";
import { RetrievalPanel, TraceInspector } from "./retrieval-panel";
import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  ArrowRight,
  ArrowUp,
  BookOpen,
  Check,
  ChevronRight,
  CircleAlert,
  Copy,
  Database,
  FileText,
  FolderClosed,
  Layers3,
  Loader2,
  Menu,
  MessageSquare,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  Trash2,
  Upload,
  X,
} from "lucide-react";

type KnowledgeBase = {
  id: string;
  name: string;
  description?: string | null;
  status: string;
  document_count: number;
};
type SourceDocument = {
  id: string;
  filename: string;
  status: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  parsed_text?: string | null;
  parser_name?: string | null;
  error_detail?: string | null;
  job?: { state: string; error_detail?: string | null };
};
type Citation = {
  chunk_id: string;
  filename: string;
  text: string;
  char_start: number;
  char_end: number;
  heading_path?: string | null;
};
type Answer = {
  trace_id: string;
  answer: string;
  refused: boolean;
  refusal_reason?: string | null;
  citations: Citation[];
};
type Health = {
  status: string;
  embedding: { configured: boolean; model: string; dimension: number };
  llm: { configured: boolean; model: string };
};
type Preview = {
  filename: string;
  text: string;
  detail: string;
  error?: string | null;
};
const STATUS: Record<string, string> = {
  ready: "已就绪",
  queued: "排队中",
  processing: "处理中",
  failed: "处理失败",
  deleted: "已删除",
};

function sizeLabel(bytes: number) {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.ceil(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function extension(filename: string) {
  return filename.split(".").pop()?.toUpperCase() ?? "FILE";
}

function AnswerContent({
  answer,
  onCitation,
}: {
  answer: Answer;
  onCitation: (citation: Citation) => void;
}) {
  let markdown = answer.answer;
  answer.citations.forEach((citation, index) => {
    markdown = markdown
      .split(`[${citation.chunk_id}]`)
      .join(`[${index + 1}](#citation-${citation.chunk_id})`);
  });
  return (
    <div className="answer-text">
      <ReactMarkdown
        skipHtml
        components={{
          img: ({ alt }) => <span>{alt}</span>,
          a: ({ href, children }) => {
            const citation = answer.citations.find(
              (item) => href === `#citation-${item.chunk_id}`,
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

function Modal({
  title,
  children,
  onClose,
  busy = false,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "modal-wide" : ""}`}
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <button
          className="icon-button"
          title="关闭"
          aria-label="关闭"
          disabled={busy}
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </div>
      {children}
    </dialog>
  );
}

export default function Home() {
  const [libraries, setLibraries] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [documents, setDocuments] = useState<SourceDocument[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [view, setView] = useState<"documents" | "answers" | "retrieval">(
    "documents",
  );
  const [traceId, setTraceId] = useState<string | null>(null);
  const [librarySearch, setLibrarySearch] = useState("");
  const [documentSearch, setDocumentSearch] = useState("");
  const [trash, setTrash] = useState(false);
  const [loading, setLoading] = useState(true);
  const [libraryLoading, setLibraryLoading] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [modal, setModal] = useState<"create" | "upload" | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState("");
  const [notice, setNotice] = useState<{
    text: string;
    error?: boolean;
  } | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [question, setQuestion] = useState("");
  const [submittedQuestion, setSubmittedQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [asking, setAsking] = useState(false);
  const selectedRef = useRef(selectedId);
  const documentRequest = useRef(0);
  const answerRequest = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const answerEnd = useRef<HTMLDivElement>(null);
  selectedRef.current = selectedId;

  const notifyError = useCallback((error: unknown) => {
    setNotice({
      text: error instanceof Error ? error.message : "请求失败，请稍后重试",
      error: true,
    });
  }, []);
  const loadLibraries = useCallback(async () => {
    try {
      const data = await request<KnowledgeBase[]>("/knowledge-bases");
      setLibraries(data);
      setSelectedId((id) => id || data[0]?.id || "");
    } finally {
      setLibraryLoading(false);
    }
  }, []);
  const loadDocuments = useCallback(async (id: string, quiet = false) => {
    const sequence = ++documentRequest.current;
    if (!quiet) setLoading(true);
    try {
      const data = await request<SourceDocument[]>(
        `/knowledge-bases/${id}/documents`,
      );
      if (sequence === documentRequest.current && selectedRef.current === id)
        setDocuments(data);
    } finally {
      if (sequence === documentRequest.current && selectedRef.current === id)
        setLoading(false);
    }
  }, []);
  useEffect(() => {
    loadLibraries().catch(notifyError);
    request<Health>("/health")
      .then(setHealth)
      .catch(() => setHealth(null));
    return () => answerRequest.current?.abort();
  }, [loadLibraries, notifyError]);
  useEffect(() => {
    answerRequest.current?.abort();
    setAsking(false);
    setAnswer(null);
    setQuestion("");
    setSubmittedQuestion("");
    setDocuments([]);
    setPreview(null);
    setTraceId(null);
    setDocumentSearch("");
    setTrash(false);
    if (selectedId) loadDocuments(selectedId).catch(notifyError);
    else setLoading(false);
  }, [selectedId, loadDocuments, notifyError]);
  useEffect(() => {
    if (
      !selectedId ||
      !documents.some((doc) => ["queued", "processing"].includes(doc.status))
    )
      return;
    const timer = setInterval(() => {
      loadDocuments(selectedId, true).catch(notifyError);
      loadLibraries().catch(notifyError);
    }, 3000);
    return () => clearInterval(timer);
  }, [documents, selectedId, loadDocuments, loadLibraries, notifyError]);
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (answer || asking)
      answerEnd.current?.scrollIntoView({
        block: "nearest",
        behavior: "smooth",
      });
  }, [answer, asking]);

  function openModal(next: "create" | "upload") {
    setModalError("");
    setFile(null);
    setModal(next);
  }
  async function refresh(id: string) {
    await Promise.all([
      selectedRef.current === id ? loadDocuments(id, true) : Promise.resolve(),
      loadLibraries(),
    ]);
  }
  async function createLibrary(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setModalError("");
    try {
      const created = await request<KnowledgeBase>("/knowledge-bases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
        }),
      });
      setLibraries((items) => [created, ...items]);
      setSelectedId(created.id);
      setView("documents");
      setName("");
      setDescription("");
      setModal(null);
      setNotice({ text: "知识库已创建" });
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }
  function chooseFile(next?: File) {
    setModalError("");
    if (next && next.size > 100 * 1024 * 1024) {
      setModalError("文件大小不能超过 100 MB");
      setFile(null);
      return;
    }
    setFile(next ?? null);
  }
  async function uploadDocument(event: FormEvent) {
    event.preventDefault();
    if (!file || !selectedId) return;
    const id = selectedId;
    setBusy(true);
    setModalError("");
    try {
      const body = new FormData();
      body.append("file", file);
      await request(`/knowledge-bases/${id}/documents`, {
        method: "POST",
        body,
      });
      setFile(null);
      setModal(null);
      setNotice({ text: "上传成功，文档已进入处理队列" });
      await refresh(id);
    } catch (error) {
      const text = error instanceof Error ? error.message : "上传失败";
      setModalError(text);
      setNotice({ text, error: true });
    } finally {
      setBusy(false);
    }
  }
  async function updateDocument(
    doc: SourceDocument,
    action: "delete" | "restore",
  ) {
    const id = selectedId;
    setBusy(true);
    try {
      await request(
        `/documents/${doc.id}${action === "restore" ? "/restore" : ""}`,
        { method: action === "delete" ? "DELETE" : "POST" },
      );
      if (selectedRef.current === id) {
        answerRequest.current?.abort();
        setAsking(false);
        setAnswer(null);
        setSubmittedQuestion("");
      }
      setNotice({
        text: action === "delete" ? "文档已移至回收站" : "文档已恢复",
      });
      await refresh(id);
    } catch (error) {
      notifyError(error);
    } finally {
      setBusy(false);
    }
  }
  async function askQuestion(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || asking) return;
    const id = selectedId;
    const controller = new AbortController();
    answerRequest.current = controller;
    const query = question.trim();
    setSubmittedQuestion(query);
    setQuestion("");
    setAnswer(null);
    setAsking(true);
    try {
      const data = await request<Answer>(`/knowledge-bases/${id}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, top_k: 6 }),
        signal: controller.signal,
      });
      if (selectedRef.current === id && !controller.signal.aborted)
        setAnswer(data);
    } catch (error) {
      if (!controller.signal.aborted && selectedRef.current === id) {
        notifyError(error);
        setQuestion(query);
      }
    } finally {
      if (answerRequest.current === controller) setAsking(false);
    }
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNotice({ text: "已复制" });
    } catch {
      setNotice({ text: "浏览器不允许访问剪贴板", error: true });
    }
  }
  function showDocument(doc: SourceDocument) {
    setPreview({
      filename: doc.filename,
      text: doc.parsed_text || "暂无解析内容",
      detail: `${extension(doc.filename)} · ${sizeLabel(doc.size_bytes)}${doc.parser_name ? ` · ${doc.parser_name}` : ""}`,
      error: doc.error_detail || doc.job?.error_detail,
    });
  }
  function showCitation(citation: Citation) {
    setPreview({
      filename: citation.filename,
      text: citation.text,
      detail: `${citation.heading_path ? `${citation.heading_path} · ` : ""}字符 ${citation.char_start}–${citation.char_end} · Chunk ${citation.chunk_id}`,
    });
  }
  const selected = libraries.find((item) => item.id === selectedId);
  const activeDocuments = documents.filter((doc) => doc.status !== "deleted");
  const readyCount = activeDocuments.filter(
    (doc) => doc.status === "ready",
  ).length;
  const processingCount = activeDocuments.filter((doc) =>
    ["queued", "processing"].includes(doc.status),
  ).length;
  const visibleDocuments = documents.filter(
    (doc) =>
      (doc.status === "deleted") === trash &&
      doc.filename.toLowerCase().includes(documentSearch.toLowerCase()),
  );
  const filteredLibraries = libraries.filter((item) =>
    item.name.toLowerCase().includes(librarySearch.toLowerCase()),
  );

  return (
    <div className="workspace">
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={`sidebar ${sidebarOpen ? "is-open" : ""}`}
        aria-label="工作空间导航"
      >
        <a className="brand" href="/">
          <span className="brand-mark">
            <Layers3 size={23} />
          </span>
          <span>
            知库<span className="brand-caption">KNOWLEDGE WORKSPACE</span>
          </span>
        </a>
        <div className="workspace-identity">
          <span className="workspace-monogram">
            <Database size={17} />
          </span>
          <div>
            <strong>我的工作空间</strong>
            <small>本地部署</small>
          </div>
          <button
            className="icon-button mobile-only"
            aria-label="关闭导航"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        <div className="sidebar-section-heading">
          <span>知识库</span>
          <span className="sidebar-heading-actions">
            <span>{libraries.length}</span>
            <button
              className="icon-button"
              title="新建知识库"
              aria-label="新建知识库"
              onClick={() => openModal("create")}
            >
              <Plus size={16} />
            </button>
          </span>
        </div>
        <label className="search-field sidebar-search">
          <Search size={15} />
          <input
            aria-label="搜索知识库"
            placeholder="搜索知识库"
            value={librarySearch}
            onChange={(event) => setLibrarySearch(event.target.value)}
          />
        </label>
        <nav className="library-list" aria-label="知识库">
          {filteredLibraries.map((library) => (
            <button
              key={library.id}
              className={`library-item ${selectedId === library.id ? "selected" : ""}`}
              aria-current={selectedId === library.id ? "page" : undefined}
              onClick={() => {
                setSelectedId(library.id);
                setSidebarOpen(false);
              }}
            >
              <BookOpen size={17} />
              <span>{library.name}</span>
              <small>{library.document_count}</small>
            </button>
          ))}
          {libraryLoading && <p className="sidebar-empty">正在加载…</p>}
          {!libraryLoading && !filteredLibraries.length && (
            <p className="sidebar-empty">
              {librarySearch ? "没有匹配的知识库" : "暂无知识库"}
            </p>
          )}
        </nav>
        <button className="create-library" onClick={() => openModal("create")}>
          <Plus size={16} />
          新建知识库
        </button>
        <div className="sidebar-footer">
          <Server size={16} />
          <div>
            <strong>本地 Docker</strong>
            <small>
              <span
                className={`status-dot ${health?.status === "ok" ? "online" : ""}`}
              />
              {health?.status === "ok" ? "服务运行中" : "服务状态待检查"}
            </small>
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="breadcrumbs">
            <button
              className="icon-button mobile-only"
              title="打开导航"
              aria-label="打开导航"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={19} />
            </button>
            <span>工作空间</span>
            <ChevronRight size={14} />
            <strong>{selected?.name || "知识库"}</strong>
          </div>
          <span className="environment">
            <span className="status-dot online" />
            LOCAL
          </span>
        </header>
        {selected ? (
          <>
            <div className="page-heading">
              <div className="library-symbol">
                <BookOpen size={25} strokeWidth={1.6} />
              </div>
              <div>
                <div className="title-line">
                  <h1>{selected.name}</h1>
                  <span className="subtle-label">知识库</span>
                </div>
                <p>
                  {selected.description || `${selected.document_count} 份文档`}
                </p>
              </div>
            </div>
            <div className="view-tabs" role="tablist" aria-label="知识库视图">
              <button
                role="tab"
                aria-selected={view === "documents"}
                aria-controls="documents-view"
                id="documents-tab"
                className={view === "documents" ? "active" : ""}
                onClick={() => setView("documents")}
              >
                <FolderClosed size={16} />
                文档<span>{activeDocuments.length}</span>
              </button>
              <button
                role="tab"
                aria-selected={view === "answers"}
                aria-controls="answers-view"
                id="answers-tab"
                className={view === "answers" ? "active" : ""}
                onClick={() => setView("answers")}
              >
                <MessageSquare size={16} />
                知识问答
              </button>
              <button
                role="tab"
                aria-selected={view === "retrieval"}
                aria-controls="retrieval-view"
                id="retrieval-tab"
                className={view === "retrieval" ? "active" : ""}
                onClick={() => setView("retrieval")}
              >
                <Search size={16} />
                检索调试
              </button>
            </div>
            {view === "retrieval" ? (
              <RetrievalPanel
                key={selectedId}
                knowledgeBaseId={selectedId}
                onPreview={setPreview}
              />
            ) : view === "documents" ? (
              <section
                className="documents-view"
                id="documents-view"
                role="tabpanel"
                aria-labelledby="documents-tab"
              >
                <div className="summary-strip">
                  <div>
                    <span>文档总数</span>
                    <strong>
                      {activeDocuments.length}
                      <small>份</small>
                    </strong>
                  </div>
                  <div>
                    <span>
                      <i className="status-dot online" />
                      已就绪
                    </span>
                    <strong>{readyCount}</strong>
                  </div>
                  <div>
                    <span>
                      <i className="status-dot amber" />
                      处理中
                    </span>
                    <strong>{processingCount}</strong>
                  </div>
                  <div className="summary-note">
                    <Database size={16} />
                    <span>
                      {health?.embedding.configured
                        ? `${health.embedding.dimension} 维向量索引`
                        : "Embedding 待配置"}
                    </span>
                  </div>
                </div>
                <div className="document-toolbar">
                  <div className="document-filters">
                    <button
                      className={!trash ? "active" : ""}
                      onClick={() => setTrash(false)}
                    >
                      全部文档
                    </button>
                    <button
                      className={trash ? "active" : ""}
                      onClick={() => setTrash(true)}
                    >
                      <Trash2 size={14} />
                      回收站
                      {documents.some((doc) => doc.status === "deleted") && (
                        <span>
                          {
                            documents.filter((doc) => doc.status === "deleted")
                              .length
                          }
                        </span>
                      )}
                    </button>
                  </div>
                  <div className="toolbar-actions">
                    <label className="search-field document-search">
                      <Search size={15} />
                      <input
                        aria-label="搜索文档"
                        placeholder="搜索文档…"
                        value={documentSearch}
                        onChange={(event) =>
                          setDocumentSearch(event.target.value)
                        }
                      />
                    </label>
                    <button
                      className="icon-button bordered"
                      title="刷新文档"
                      aria-label="刷新文档"
                      disabled={loading}
                      onClick={() =>
                        loadDocuments(selectedId).catch(notifyError)
                      }
                    >
                      <RefreshCw size={16} className={loading ? "spin" : ""} />
                    </button>
                    <button
                      className="primary"
                      onClick={() => openModal("upload")}
                    >
                      <Plus size={16} />
                      上传文档
                    </button>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>文档名称</th>
                        <th>状态</th>
                        <th className="size-column">大小</th>
                        <th className="date-column">添加时间</th>
                        <th className="actions-column">
                          <span className="sr-only">操作</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {loading
                        ? Array.from({ length: 4 }, (_, index) => (
                            <tr key={index} aria-hidden="true">
                              <td colSpan={5}>
                                <div className="skeleton" />
                              </td>
                            </tr>
                          ))
                        : visibleDocuments.map((doc) => (
                            <tr key={doc.id}>
                              <td>
                                <button
                                  className="document-name"
                                  onClick={() => showDocument(doc)}
                                  title={doc.filename}
                                >
                                  <span
                                    className={`file-symbol file-${extension(doc.filename).toLowerCase()}`}
                                  >
                                    <FileText size={21} strokeWidth={1.5} />
                                  </span>
                                  <span className="filename">
                                    <strong>{doc.filename}</strong>
                                    <small>
                                      {extension(doc.filename)} 文档
                                    </small>
                                  </span>
                                </button>
                              </td>
                              <td>
                                <span className={`status-badge ${doc.status}`}>
                                  <span className="status-dot" />
                                  {STATUS[doc.status] || doc.status}
                                </span>
                              </td>
                              <td className="size-column metadata">
                                {sizeLabel(doc.size_bytes)}
                              </td>
                              <td className="date-column metadata">
                                {new Date(doc.created_at).toLocaleDateString(
                                  "zh-CN",
                                  {
                                    year: "numeric",
                                    month: "2-digit",
                                    day: "2-digit",
                                  },
                                )}
                              </td>
                              <td>
                                <div className="row-actions">
                                  <button
                                    className="icon-button preview-action"
                                    title="查看原文"
                                    aria-label={`查看 ${doc.filename}`}
                                    onClick={() => showDocument(doc)}
                                  >
                                    <BookOpen size={16} />
                                  </button>
                                  <button
                                    className={`icon-button ${trash ? "" : "danger"}`}
                                    title={trash ? "恢复文档" : "移至回收站"}
                                    aria-label={`${trash ? "恢复" : "删除"} ${doc.filename}`}
                                    disabled={busy}
                                    onClick={() =>
                                      updateDocument(
                                        doc,
                                        trash ? "restore" : "delete",
                                      )
                                    }
                                  >
                                    {trash ? (
                                      <RotateCcw size={16} />
                                    ) : (
                                      <Trash2 size={16} />
                                    )}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ))}
                    </tbody>
                  </table>
                  {!loading && !visibleDocuments.length && (
                    <div className="empty-state">
                      <span className="empty-icon">
                        {trash ? (
                          <Trash2 size={28} strokeWidth={1.4} />
                        ) : (
                          <FolderClosed size={30} strokeWidth={1.4} />
                        )}
                      </span>
                      <h2>
                        {documentSearch
                          ? "没有匹配的文档"
                          : trash
                            ? "回收站为空"
                            : "还没有文档"}
                      </h2>
                      {!trash && !documentSearch && (
                        <button
                          className="text-button"
                          onClick={() => openModal("upload")}
                        >
                          上传文档
                          <ArrowRight size={15} />
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <footer className="table-footer">
                  <span>
                    {loading
                      ? "正在加载文档…"
                      : `共 ${visibleDocuments.length} 份${trash ? "已删除" : ""}文档`}
                  </span>
                  <span>
                    <Check size={13} />
                    {health?.embedding.configured
                      ? health.embedding.model
                      : "未连接向量模型"}
                  </span>
                </footer>
              </section>
            ) : (
              <section
                className="answers-view"
                id="answers-view"
                role="tabpanel"
                aria-labelledby="answers-tab"
              >
                <div className="conversation">
                  {!submittedQuestion ? (
                    <div className="answer-empty">
                      <span className="answer-emblem">
                        <Layers3 size={32} strokeWidth={1.4} />
                      </span>
                      <h2>{selected.name}</h2>
                      <p>
                        {readyCount} 份就绪文档<span>·</span>知识问答
                      </p>
                      <div className="answer-divider" />
                      <span className="grounding-label">
                        <BookOpen size={14} />
                        基于知识库原文
                      </span>
                    </div>
                  ) : (
                    <div className="exchange">
                      <div className="question-block">
                        <span className="message-label">你</span>
                        <p>{submittedQuestion}</p>
                      </div>
                      <div className="response-block">
                        <span className="message-label">
                          <Layers3 size={16} />
                          知库
                          {answer && (
                            <span
                              className={`answer-label ${answer.refused ? "refused" : ""}`}
                            >
                              {answer.refused ? "证据不足" : "回答完成"}
                            </span>
                          )}
                        </span>
                        {asking && (
                          <div className="thinking">
                            <Loader2 size={17} className="spin" />
                            正在检索资料并生成回答…
                          </div>
                        )}
                        {answer && (
                          <>
                            <AnswerContent
                              answer={answer}
                              onCitation={showCitation}
                            />
                            {answer.citations.length > 0 && (
                              <div className="sources">
                                <div className="sources-heading">
                                  参考来源<span>{answer.citations.length}</span>
                                </div>
                                <div className="source-list">
                                  {answer.citations.map((citation, index) => (
                                    <button
                                      className="source-chip"
                                      key={citation.chunk_id}
                                      onClick={() => showCitation(citation)}
                                      title={citation.filename}
                                    >
                                      <span>{index + 1}</span>
                                      <FileText size={14} />
                                      <strong>{citation.filename}</strong>
                                      <ChevronRight size={14} />
                                    </button>
                                  ))}
                                </div>
                              </div>
                            )}
                            <div className="answer-actions">
                              <button
                                className="icon-button"
                                title="复制回答"
                                aria-label="复制回答"
                                onClick={() => copy(answer.answer)}
                              >
                                <Copy size={15} />
                              </button>
                              <button
                                className="trace-button"
                                title={answer.trace_id}
                                aria-label="查看回答检索记录"
                                onClick={() => setTraceId(answer.trace_id)}
                              >
                                TRACE {answer.trace_id.slice(0, 8)}
                                <ChevronRight size={11} />
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  )}
                  <div ref={answerEnd} />
                </div>
                <div className="composer-area">
                  <form className="composer" onSubmit={askQuestion}>
                    <textarea
                      aria-label="向知识库提问"
                      placeholder={`向「${selected.name}」提问…`}
                      rows={2}
                      value={question}
                      maxLength={4000}
                      disabled={asking}
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
                        {selected.name}
                      </span>
                      <button
                        className="send-button"
                        title="发送问题"
                        aria-label="发送问题"
                        disabled={asking || !question.trim()}
                        type="submit"
                      >
                        {asking ? (
                          <Loader2 size={18} className="spin" />
                        ) : (
                          <ArrowUp size={19} />
                        )}
                      </button>
                    </div>
                  </form>
                  <div className="composer-meta">
                    <span>
                      <span
                        className={`status-dot ${health?.llm.configured ? "online" : "amber"}`}
                      />
                      {health?.llm.configured
                        ? health.llm.model
                        : "回答模型未配置"}
                    </span>
                    <span>AI 回答仅供参考，请核对来源</span>
                  </div>
                </div>
              </section>
            )}
          </>
        ) : (
          <div className="workspace-empty">
            <Layers3 size={36} strokeWidth={1.4} />
            <h1>{libraryLoading ? "正在加载工作空间…" : "我的知识库"}</h1>
            {!libraryLoading && (
              <button className="primary" onClick={() => openModal("create")}>
                <Plus size={16} />
                新建知识库
              </button>
            )}
          </div>
        )}
      </main>
      {traceId && (
        <Modal title="检索记录" wide onClose={() => setTraceId(null)}>
          <TraceInspector
            key={traceId}
            traceId={traceId}
            knowledgeBaseId={selectedId}
          />
        </Modal>
      )}
      {notice && (
        <div
          className={`toast ${notice.error ? "error" : ""}`}
          role={notice.error ? "alert" : "status"}
        >
          {notice.error ? <CircleAlert size={17} /> : <Check size={17} />}
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
      {modal === "create" && (
        <Modal title="新建知识库" onClose={() => setModal(null)} busy={busy}>
          <form className="modal-form" onSubmit={createLibrary}>
            <label>
              名称
              <input
                autoFocus
                required
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：产品文档"
              />
            </label>
            <label>
              描述<span className="optional">选填</span>
              <textarea
                rows={3}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="知识库描述"
              />
            </label>
            {modalError && (
              <p className="form-error" role="alert">
                {modalError}
              </p>
            )}
            <div className="modal-footer">
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => setModal(null)}
              >
                取消
              </button>
              <button
                className="primary"
                type="submit"
                disabled={busy || !name.trim()}
              >
                {busy && <Loader2 size={15} className="spin" />}创建知识库
              </button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "upload" && (
        <Modal title="上传文档" onClose={() => setModal(null)} busy={busy}>
          <form className="modal-form" onSubmit={uploadDocument}>
            <div className="upload-destination">
              <BookOpen size={15} />
              <span>{selected?.name}</span>
            </div>
            <label
              className={`dropzone ${file ? "has-file" : ""}`}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                if (!busy) chooseFile(event.dataTransfer.files[0]);
              }}
            >
              <input
                ref={fileInput}
                aria-label="选择文件"
                type="file"
                accept=".pdf,.docx,.md,.txt,.html,.csv,.xlsx"
                disabled={busy}
                onChange={(event) => chooseFile(event.target.files?.[0])}
              />
              <span className="upload-symbol">
                {file ? <FileText size={26} /> : <Upload size={26} />}
              </span>
              <strong>{file ? file.name : "选择文件或拖拽到此处"}</strong>
              <span>
                {file
                  ? sizeLabel(file.size)
                  : "PDF、DOCX、MD、TXT、HTML、CSV、XLSX"}
              </span>
              <small>单个文件最大 100 MB</small>
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
                disabled={busy}
                onClick={() => setModal(null)}
              >
                取消
              </button>
              <button
                className="primary"
                disabled={busy || !file}
                type="submit"
              >
                {busy ? (
                  <Loader2 size={15} className="spin" />
                ) : (
                  <Upload size={15} />
                )}
                {busy ? "上传中…" : "上传文档"}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {preview && (
        <Modal title="原文详情" onClose={() => setPreview(null)} wide>
          <div className="preview-heading">
            <span className="file-symbol">
              <FileText size={22} />
            </span>
            <div>
              <h3>{preview.filename}</h3>
              <p>{preview.detail}</p>
            </div>
          </div>
          {preview.error && <p className="form-error">{preview.error}</p>}
          <div className="preview-text">{preview.text}</div>
        </Modal>
      )}
    </div>
  );
}
