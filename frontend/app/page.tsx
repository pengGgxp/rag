"use client";

import { ChatWorkspace } from "./chat-workspace";
import { Modal } from "./modal";
import { request } from "./api";
import { AccountButton, LibraryAccess, useAuth } from "./auth";
import { RetrievalPanel } from "./retrieval-panel";
import { useUiStore } from "./ui-store";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronRight,
  CircleAlert,
  FileText,
  FolderClosed,
  History,
  Layers3,
  Loader2,
  Menu,
  MessageSquare,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  Server,
  Trash2,
  Upload,
  XCircle,
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
  version_group_id: string;
  version_number: number;
  is_active: boolean;
  supersedes_id?: string | null;
  status: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  parsed_text?: string | null;
  parser_name?: string | null;
  error_detail?: string | null;
  job?: { id: string; state: string; error_detail?: string | null };
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

export default function Home() {
  const { role } = useAuth();
  const canEdit = role === "admin" || role === "editor";
  const [libraries, setLibraries] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [documents, setDocuments] = useState<SourceDocument[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const [screen, setScreen] = useState<"catalog" | "knowledge" | "chat">(
    "catalog",
  );
  const [chatEntry, setChatEntry] = useState(0);
  const [view, setView] = useState<"documents" | "retrieval" | "settings">(
    "documents",
  );
  const [diagnostics, setDiagnostics] = useState(false);
  const [librarySearch, setLibrarySearch] = useState("");
  const [documentSearch, setDocumentSearch] = useState("");
  const [trash, setTrash] = useState(false);
  const [loading, setLoading] = useState(true);
  const [libraryLoading, setLibraryLoading] = useState(true);
  const sidebarOpen = useUiStore((state) => state.sidebarOpen);
  const setSidebarOpen = useUiStore((state) => state.setSidebarOpen);
  const [modal, setModal] = useState<"create" | "upload" | "versions" | null>(null);
  const [versionTarget, setVersionTarget] = useState<SourceDocument | null>(null);
  const [versions, setVersions] = useState<SourceDocument[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
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
  const selectedRef = useRef(selectedId);
  const documentRequest = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  selectedRef.current = selectedId;

  const notifyError = useCallback((error: unknown) => {
    setNotice({
      text: error instanceof Error ? error.message : "请求失败，请稍后重试",
      error: true,
    });
  }, []);
  const refreshHealth = useCallback(async () => {
    setHealthLoading(true);
    try {
      setHealth(await request<Health>("/health"));
    } catch {
      setHealth(null);
    } finally {
      setHealthLoading(false);
    }
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
    if (
      new URLSearchParams(window.location.search).has("conversation") ||
      new URLSearchParams(window.location.search).has("chat")
    )
      setScreen("chat");
    loadLibraries().catch(notifyError);
    void refreshHealth();
  }, [loadLibraries, notifyError, refreshHealth]);
  useEffect(() => {
    setDocuments([]);
    setPreview(null);
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

  function openModal(next: "create" | "upload") {
    setModalError("");
    setFile(null);
    setVersionTarget(null);
    setModal(next);
  }
  function openVersionUpload(doc: SourceDocument) {
    setModalError("");
    setFile(null);
    setVersionTarget(doc);
    setModal("upload");
  }
  async function openVersions(doc: SourceDocument) {
    setVersionsLoading(true);
    setModalError("");
    try {
      setVersions(await request<SourceDocument[]>(`/documents/${doc.id}/versions`));
      setVersionTarget(doc);
      setModal("versions");
    } catch (error) {
      notifyError(error);
    } finally {
      setVersionsLoading(false);
    }
  }
  async function activateVersion(doc: SourceDocument) {
    setBusy(true);
    try {
      await request(`/documents/${doc.id}/activate`, { method: "POST" });
      setNotice({ text: `已切换到 v${doc.version_number}` });
      setModal(null);
      await refresh(selectedId);
    } catch (error) {
      notifyError(error);
    } finally {
      setBusy(false);
    }
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
      setScreen("knowledge");
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
      if (versionTarget) body.append("version_of", versionTarget.id);
      await request(`/knowledge-bases/${id}/documents`, {
        method: "POST",
        body,
      });
      setFile(null);
      setModal(null);
      setNotice({
        text: versionTarget
          ? "新版本已上传，处理完成后可切换生效版本"
          : "上传成功，文档已进入处理队列",
      });
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
  async function updateJob(doc: SourceDocument, action: "retry" | "cancel") {
    if (!doc.job) return;
    setBusy(true);
    try {
      await request(`/jobs/${doc.job.id}/${action}`, { method: "POST" });
      setNotice({ text: action === "retry" ? "任务已重新排队" : "任务已取消" });
      await refresh(selectedId);
    } catch (error) {
      notifyError(error);
    } finally {
      setBusy(false);
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
  function openLibrary(
    id: string,
    destination: "knowledge" | "chat" = "knowledge",
  ) {
    setSelectedId(id);
    if (destination === "chat") setChatEntry((value) => value + 1);
    setScreen(destination);
    setView("documents");
    setSidebarOpen(false);
  }
  const selected = libraries.find((item) => item.id === selectedId);
  const activeDocuments = documents.filter((doc) => doc.status !== "deleted");
  const visibleDocuments = documents.filter(
    (doc) =>
      (doc.status === "deleted") === trash &&
      doc.filename.toLowerCase().includes(documentSearch.toLowerCase()),
  );
  const filteredLibraries = libraries.filter((item) =>
    item.name.toLowerCase().includes(librarySearch.toLowerCase()),
  );

  if (screen === "chat")
    return (
      <ChatWorkspace
        key={chatEntry}
        libraries={libraries}
        initialLibraryId={selectedId}
        onKnowledge={() => setScreen("catalog")}
      />
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
        <a
          className="brand"
          href="/"
          onClick={(event) => {
            event.preventDefault();
            setScreen("catalog");
            setSidebarOpen(false);
          }}
        >
          <span className="brand-mark">
            <Layers3 size={23} />
          </span>
          <span>知库</span>
        </a>
        <nav className="primary-navigation" aria-label="产品导航">
          <button
            className="selected"
            onClick={() => {
              setScreen("catalog");
              setSidebarOpen(false);
            }}
          >
            <BookOpen size={18} />
            知识库
          </button>
          <button
            onClick={() => {
              if (selectedId) openLibrary(selectedId, "chat");
              else {
                setScreen("chat");
                setSidebarOpen(false);
              }
            }}
          >
            <MessageSquare size={18} />
            知识问答
          </button>
          <button
            className="icon-button mobile-only"
            aria-label="关闭导航"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={18} />
          </button>
        </nav>
        {screen !== "catalog" && (
          <>
            {screen === "knowledge" && selected && (
              <nav className="knowledge-navigation" aria-label="知识库管理">
                <button
                  className="back-link"
                  onClick={() => {
                    setScreen("catalog");
                    setSidebarOpen(false);
                  }}
                >
                  <ArrowLeft size={15} />
                  全部知识库
                </button>
                <div className="knowledge-navigation-title">
                  <BookOpen size={18} />
                  <strong>{selected.name}</strong>
                </div>
                <button
                  className={view === "documents" ? "selected" : ""}
                  onClick={() => {
                    setView("documents");
                    setSidebarOpen(false);
                  }}
                  aria-current={view === "documents" ? "page" : undefined}
                >
                  <FolderClosed size={16} />
                  文档<span>{activeDocuments.length}</span>
                </button>
                <button
                  className={view === "retrieval" ? "selected" : ""}
                  onClick={() => {
                    setView("retrieval");
                    setSidebarOpen(false);
                  }}
                  aria-current={view === "retrieval" ? "page" : undefined}
                >
                  <Search size={16} />
                  检索测试
                </button>
                <button
                  className={view === "settings" ? "selected" : ""}
                  onClick={() => {
                    setView("settings");
                    setSidebarOpen(false);
                    void refreshHealth();
                  }}
                  aria-current={view === "settings" ? "page" : undefined}
                >
                  <Settings2 size={16} />
                  配置
                </button>
              </nav>
            )}
            <div className="sidebar-section-heading">
              <span>切换知识库</span>
              <span className="sidebar-heading-actions">
                <span>{libraries.length}</span>
                <button
                  className="icon-button"
                  title="新建知识库"
                  aria-label="新建知识库"
                  disabled={!canEdit}
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
                    openLibrary(library.id);
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
            <button
              className="create-library"
              disabled={!canEdit}
              onClick={() => openModal("create")}
            >
              <Plus size={16} />
              新建知识库
            </button>
          </>
        )}
        <div className="sidebar-footer">
          <AccountButton />
          {role === "admin" && (
            <button
              className="diagnostics-link"
              onClick={() => {
                setDiagnostics(true);
                void refreshHealth();
              }}
            >
              <Settings2 size={16} />
              系统诊断
            </button>
          )}
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
            <button
              className="breadcrumb-link"
              onClick={() => setScreen("catalog")}
            >
              知识库
            </button>
            {screen !== "catalog" && (
              <>
                <ChevronRight size={14} />
                <strong>{selected?.name || "知识库"}</strong>
              </>
            )}
          </div>
          {screen === "knowledge" && selected && (
            <button
              className="secondary"
              onClick={() => openLibrary(selectedId, "chat")}
            >
              <MessageSquare size={15} />
              前往问答
            </button>
          )}
        </header>
        {screen === "catalog" ? (
          <section className="catalog" aria-label="知识库列表">
            <div className="catalog-heading">
              <h1>知识库</h1>
              <button
                className="primary"
                disabled={!canEdit}
                onClick={() => openModal("create")}
              >
                <Plus size={16} />
                新建知识库
              </button>
            </div>
            <div className="catalog-toolbar">
              <span>
                全部知识库 <span className="count">{libraries.length}</span>
              </span>
              <label className="search-field">
                <Search size={15} />
                <input
                  aria-label="搜索知识库"
                  placeholder="搜索知识库"
                  value={librarySearch}
                  onChange={(event) => setLibrarySearch(event.target.value)}
                />
              </label>
            </div>
            <div className="library-grid">
              {filteredLibraries.map((library) => (
                <button
                  className="library-card"
                  key={library.id}
                  onClick={() => openLibrary(library.id)}
                >
                  <span className="catalog-library-icon">
                    <BookOpen size={23} strokeWidth={1.7} />
                  </span>
                  <h2>{library.name}</h2>
                  <p>{library.description || "暂无描述"}</p>
                  <span className="library-card-footer">
                    <span>
                      <FileText size={14} />
                      {library.document_count} 份文档
                    </span>
                    <ChevronRight size={16} />
                  </span>
                </button>
              ))}
            </div>
            {!filteredLibraries.length && (
              <div className="empty-state">
                <BookOpen size={30} />
                <h2>
                  {libraryLoading
                    ? "正在加载…"
                    : librarySearch
                      ? "没有匹配的知识库"
                      : "暂无知识库"}
                </h2>
              </div>
            )}
          </section>
        ) : selected ? (
          <>
            <div className="page-heading">
              <div className="library-symbol">
                <BookOpen size={25} strokeWidth={1.6} />
              </div>
              <div>
                <div className="title-line">
                  <h1>{selected.name}</h1>
                  <span className="subtle-label">
                    {view === "retrieval"
                      ? "检索测试"
                      : view === "settings"
                        ? "配置"
                        : "文档"}
                  </span>
                </div>
                <p>
                  {selected.description || `${selected.document_count} 份文档`}
                </p>
              </div>
            </div>
            {screen === "knowledge" && view === "settings" ? (
              <section className="configuration" aria-label="知识库配置">
                <h2>基本信息</h2>
                <dl className="configuration-fields">
                  <div>
                    <dt>名称</dt>
                    <dd>{selected.name}</dd>
                  </div>
                  <div>
                    <dt>描述</dt>
                    <dd>{selected.description || "暂无描述"}</dd>
                  </div>
                </dl>
                <h2>当前服务配置</h2>
                <dl className="configuration-fields">
                  <div>
                    <dt>检索方式</dt>
                    <dd>混合检索</dd>
                  </div>
                  <div>
                    <dt>向量模型</dt>
                    <dd>
                      {healthLoading
                        ? "加载中…"
                        : !health
                          ? "无法获取配置"
                          : health.embedding.model || "未配置"}
                    </dd>
                  </div>
                  <div>
                    <dt>向量维度</dt>
                    <dd>
                      {healthLoading
                        ? "加载中…"
                        : (health?.embedding.dimension ?? "—")}
                    </dd>
                  </div>
                  <div>
                    <dt>回答模型</dt>
                    <dd>
                      {healthLoading
                        ? "加载中…"
                        : !health
                          ? "无法获取配置"
                          : health.llm.model || "未配置"}
                    </dd>
                  </div>
                </dl>
                {role === "admin" && (
                  <LibraryAccess
                    key={selectedId}
                    id={selectedId}
                    onChange={() => void loadLibraries().catch(notifyError)}
                  />
                )}
              </section>
            ) : screen === "knowledge" && view === "retrieval" ? (
              <RetrievalPanel
                key={selectedId}
                knowledgeBaseId={selectedId}
                onPreview={setPreview}
              />
            ) : (
              <section
                className="documents-view"
                id="documents-view"
                aria-label="文档"
              >
                <div className="document-toolbar">
                  <div className="document-filters">
                    <button
                      className={!trash ? "active" : ""}
                      onClick={() => setTrash(false)}
                    >
                      全部文档
                      <span>{activeDocuments.length}</span>
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
                      disabled={!canEdit}
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
                                      {extension(doc.filename)} 文档 · v{doc.version_number}
                                      {doc.is_active ? " · 当前生效" : ""}
                                    </small>
                                  </span>
                                </button>
                              </td>
                              <td>
                                <span
                                  className={`status-badge ${doc.status}`}
                                  title={doc.error_detail || doc.job?.error_detail || undefined}
                                >
                                  <span className="status-dot" />
                                  {STATUS[doc.status] || doc.status}
                                </span>
                                {(doc.error_detail || doc.job?.error_detail) && (
                                  <small className="document-error">
                                    {doc.error_detail || doc.job?.error_detail}
                                  </small>
                                )}
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
                                    disabled={busy || !canEdit}
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
                                  {!trash && canEdit && (
                                    <>
                                      {doc.job &&
                                        ["failed", "cancelled"].includes(
                                          doc.job.state,
                                        ) && (
                                          <button
                                            className="icon-button"
                                            title="重试处理"
                                            aria-label={`重试 ${doc.filename} 的处理`}
                                            disabled={busy}
                                            onClick={() =>
                                              void updateJob(doc, "retry")
                                            }
                                          >
                                            <RotateCcw size={16} />
                                          </button>
                                        )}
                                      {doc.job &&
                                        ["queued", "running"].includes(
                                          doc.job.state,
                                        ) && (
                                          <button
                                            className="icon-button danger"
                                            title="取消处理"
                                            aria-label={`取消 ${doc.filename} 的处理`}
                                            disabled={busy}
                                            onClick={() =>
                                              void updateJob(doc, "cancel")
                                            }
                                          >
                                            <XCircle size={16} />
                                          </button>
                                        )}
                                      <button
                                        className="icon-button"
                                        title="上传新版本"
                                        aria-label={`为 ${doc.filename} 上传新版本`}
                                        disabled={busy}
                                        onClick={() => openVersionUpload(doc)}
                                      >
                                        <Upload size={16} />
                                      </button>
                                      <button
                                        className="icon-button"
                                        title="查看版本"
                                        aria-label={`查看 ${doc.filename} 的版本`}
                                        disabled={busy}
                                        onClick={() => void openVersions(doc)}
                                      >
                                        <History size={16} />
                                      </button>
                                    </>
                                  )}
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
                          disabled={!canEdit}
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
                </footer>
              </section>
            )}
          </>
        ) : (
          <div className="workspace-empty">
            <Layers3 size={36} strokeWidth={1.4} />
            <h1>{libraryLoading ? "正在加载工作空间…" : "我的知识库"}</h1>
            {!libraryLoading && (
              <button
                className="primary"
                disabled={!canEdit}
                onClick={() => openModal("create")}
              >
                <Plus size={16} />
                新建知识库
              </button>
            )}
          </div>
        )}
      </main>
      {diagnostics && (
        <Modal title="系统诊断" onClose={() => setDiagnostics(false)}>
          <div className="diagnostic-content">
            <Server size={22} />
            <dl className="configuration-fields">
              <div>
                <dt>连接状态</dt>
                <dd>
                  {healthLoading
                    ? "检查中…"
                    : health?.status === "ok"
                      ? "正常"
                      : "暂不可用"}
                </dd>
              </div>
              <div>
                <dt>向量服务</dt>
                <dd>
                  {healthLoading
                    ? "检查中…"
                    : !health
                      ? "无法获取"
                      : health.embedding.configured
                        ? "已配置"
                        : "未配置"}
                </dd>
              </div>
              <div>
                <dt>回答服务</dt>
                <dd>
                  {healthLoading
                    ? "检查中…"
                    : !health
                      ? "无法获取"
                      : health.llm.configured
                        ? "已配置"
                        : "未配置"}
                </dd>
              </div>
            </dl>
          </div>
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
        <Modal
          title={versionTarget ? `上传 ${versionTarget.filename} 的新版本` : "上传文档"}
          onClose={() => setModal(null)}
          busy={busy}
        >
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
      {modal === "versions" && versionTarget && (
        <Modal
          title={`${versionTarget.filename} 的版本`}
          onClose={() => setModal(null)}
          busy={busy}
          wide
        >
          <div className="version-list">
            {versionsLoading ? (
              <p>正在加载版本…</p>
            ) : (
              versions.map((version) => (
                <div className="version-row" key={version.id}>
                  <div>
                    <strong>v{version.version_number}</strong>
                    <small>
                      {STATUS[version.status] || version.status} ·{" "}
                      {new Date(version.created_at).toLocaleString("zh-CN")}
                    </small>
                  </div>
                  <span className={version.is_active ? "version-current" : ""}>
                    {version.is_active ? "当前生效" : ""}
                  </span>
                  {!version.is_active && version.status === "ready" && canEdit && (
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => void activateVersion(version)}
                    >
                      切换生效
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
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
