"use client";

import { ChatWorkspace } from "./chat-workspace";
import { Modal } from "./modal";
import { FilePreview } from "./resource-preview";
import { request } from "./api";
import { useAuth } from "./auth";
import { RetrievalPanel } from "./retrieval-panel";
import { useUiStore } from "./ui-store";
import { WorkspaceNavigation, WorkspaceShell } from "./workspace-shell";
import { KnowledgeNavigation } from "./knowledge-navigation";
import { WorkspaceHeader, WorkspaceSkeleton } from "./workspace-header";
import { useDocumentUploads } from "./document-uploads";
import { documentIssue, documentRetryLabel } from "./workspace-types";
import type {
  Health,
  KnowledgeBase,
  KnowledgeView,
  Preview,
  SourceDocument,
  WorkspaceScreen,
} from "./workspace-types";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { AnimatePresence } from "motion/react";
import { Motion } from "../components/ui/motion";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Archive,
  BookOpen,
  Check,
  ChevronRight,
  CircleAlert,
  FileText,
  FolderClosed,
  History,
  Layers3,
  Loader2,
  MessageSquare,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  Trash2,
  Upload,
  XCircle,
  X,
} from "lucide-react";

const STATUS: Record<string, string> = {
  ready: "已就绪",
  queued: "排队中",
  processing: "处理中",
  failed: "处理失败",
  deleted: "已删除",
};
const LIBRARY_STATUS: Record<string, string> = {
  draft: "草稿",
  processing: "处理中",
  ready: "可用",
  error: "处理失败",
  archived: "已归档",
};

function sizeLabel(bytes: number) {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.ceil(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function extension(filename: string) {
  return filename.split(".").pop()?.toUpperCase() ?? "FILE";
}

export default function KnowledgeWorkspace() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const workspaceParams = useRef(searchParams);
  // Intercepted settings routes keep the workspace mounted beneath the dialog.
  if (pathname === "/") workspaceParams.current = searchParams;
  const params = workspaceParams.current;
  const { session } = useAuth();
  const uploads = useDocumentUploads();
  const canEdit = true;
  const [libraries, setLibraries] = useState<KnowledgeBase[]>([]);
  const selectedId = params.get("library") || "";
  const [documents, setDocuments] = useState<SourceDocument[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const screen: WorkspaceScreen = params.has("conversation") || params.has("chat")
    ? "chat" : selectedId ? "knowledge" : "catalog";
  useEffect(() => {
    if (pathname === "/" && params.has("extensions")) router.replace("/extensions", { scroll: false });
  }, [pathname, params, router]);
  const requestedView = params.get("view");
  const view: KnowledgeView = requestedView === "retrieval" || requestedView === "settings"
    ? requestedView : "documents";
  useEffect(() => {
    if (pathname !== "/" || screen !== "knowledge" || !requestedView ||
        ["documents", "retrieval", "settings"].includes(requestedView)) return;
    const corrected = new URLSearchParams(params.toString());
    corrected.set("view", "documents");
    router.replace(`/?${corrected.toString()}`, { scroll: false });
  }, [pathname, screen, requestedView, params, router]);
  const [librarySearch, setLibrarySearch] = useState("");
  const [documentSearch, setDocumentSearch] = useState("");
  const [trash, setTrash] = useState(false);
  const [loading, setLoading] = useState(true);
  const libraryDestination = `${screen}:${selectedId}`;
  const libraryDestinationRef = useRef(libraryDestination);
  libraryDestinationRef.current = libraryDestination;
  const libraryRequest = useRef(0);
  const [libraryLoadedFor, setLibraryLoadedFor] = useState<string | null>(null);
  const [librariesLoading, setLibraryLoading] = useState(true);
  const libraryLoading = librariesLoading || libraryLoadedFor !== libraryDestination;
  const [libraryError, setLibraryError] = useState("");
  const [documentError, setDocumentError] = useState("");
  const setSidebarOpen = useUiStore((state) => state.setSidebarOpen);
  const [modal, setModal] = useState<
    "create" | "edit" | "versions" | null
  >(null);
  const [versionTarget, setVersionTarget] = useState<SourceDocument | null>(null);
  const [versions, setVersions] = useState<SourceDocument[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState("");
  const [notice, setNotice] = useState<{
    text: string;
    error?: boolean;
  } | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [sourcePreview, setSourcePreview] = useState<SourceDocument | null>(null);
  const selectedRef = useRef(selectedId);
  const documentRequest = useRef(0);
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
    const sequence = ++libraryRequest.current;
    const isCurrent = () => sequence === libraryRequest.current &&
      libraryDestinationRef.current === libraryDestination;
    setLibraryLoading(true);
    try {
      const data = await request<KnowledgeBase[]>("/knowledge-bases");
      if (isCurrent()) {
        setLibraries(data);
        setLibraryError("");
      }
    } catch (error) {
      if (isCurrent()) {
        setLibraryError(error instanceof Error ? error.message : "知识库加载失败");
        throw error;
      }
    } finally {
      if (isCurrent()) {
        setLibraryLoadedFor(libraryDestination);
        setLibraryLoading(false);
      }
    }
  }, [libraryDestination]);
  const loadDocuments = useCallback(async (id: string, quiet = false) => {
    const sequence = ++documentRequest.current;
    if (!quiet) setLoading(true);
    try {
      const data = await request<SourceDocument[]>(
        `/knowledge-bases/${id}/documents`,
      );
      if (sequence === documentRequest.current && selectedRef.current === id) {
        setDocuments(data);
        setDocumentError("");
      }
    } catch (error) {
      if (sequence === documentRequest.current && selectedRef.current === id)
        setDocumentError(error instanceof Error ? error.message : "文档加载失败");
      throw error;
    } finally {
      if (sequence === documentRequest.current && selectedRef.current === id)
        setLoading(false);
    }
  }, []);
  useEffect(() => {
    // SPA navigation keeps this workspace mounted, including after Agent writes.
    // Re-read authorized metadata before treating a cached miss as inaccessible.
    setLibraryLoadedFor(null);
    void loadLibraries().catch(notifyError);
  }, [loadLibraries, notifyError]);
  useEffect(() => {
    void refreshHealth();
  }, [refreshHealth]);
  useEffect(() => {
    const admitted = (event: Event) => {
      const id = (event as CustomEvent<{ library: string }>).detail.library;
      if (id === selectedRef.current) void loadDocuments(id, true).catch(notifyError);
      void loadLibraries().catch(notifyError);
    };
    window.addEventListener("rag-upload-admitted", admitted);
    return () => window.removeEventListener("rag-upload-admitted", admitted);
  }, [loadDocuments, loadLibraries, notifyError]);
  useEffect(() => {
    setDocuments([]);
    setDocumentError("");
    setPreview(null);
    setSourcePreview(null);
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

  function openModal(next: "create" | "edit" | "upload") {
    if (next === "upload") {
      if (selected) uploads.open({ id: selected.id, name: selected.name });
      return;
    }
    setModalError("");
    setVersionTarget(null);
    setTagsInput("");
    setModal(next);
  }
  function openEditLibrary() {
    if (!selected) return;
    setName(selected.name);
    setDescription(selected.description || "");
    setTagsInput(selected.tags.join(", "));
    setModalError("");
    setModal("edit");
  }
  async function updateLibrary(event: FormEvent) {
    event.preventDefault();
    if (!selectedId) return;
    setBusy(true);
    setModalError("");
    try {
      await request(`/knowledge-bases/${selectedId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
          tags: tagsInput.split(",").map((tag) => tag.trim()).filter(Boolean),
        }),
      });
      setModal(null);
      setNotice({ text: "知识库信息已更新" });
      await loadLibraries();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }
  async function changeLibraryArchive(action: "archive" | "restore") {
    if (!selectedId) return;
    setBusy(true);
    try {
      await request(`/knowledge-bases/${selectedId}/${action}`, {
        method: "POST",
      });
      setNotice({ text: action === "archive" ? "知识库已归档" : "知识库已恢复" });
      await loadLibraries();
    } catch (error) {
      notifyError(error);
    } finally {
      setBusy(false);
    }
  }
  function openVersionUpload(doc: SourceDocument) {
    setModal(null);
    if (selected) uploads.open({ id: selected.id, name: selected.name, versionOf: doc.id, filename: doc.filename });
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
          tags: tagsInput.split(",").map((tag) => tag.trim()).filter(Boolean),
        }),
      });
      setLibraries((items) => [created, ...items]);
      openLibrary(created.id);
      setName("");
      setDescription("");
      setTagsInput("");
      setModal(null);
      setNotice({ text: "知识库已创建" });
    } catch (error) {
      setModalError(error instanceof Error ? error.message : "创建失败");
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
  async function showDocument(doc: SourceDocument) {
    setSourcePreview(doc);
  }
  async function showParsedDocument(doc: SourceDocument) {
    setPreview({
      id: doc.id,
      filename: doc.filename,
      text: "正在加载解析内容…",
      detail: `${extension(doc.filename)} · ${sizeLabel(doc.size_bytes)}${doc.parser_name ? ` · ${doc.parser_name}` : ""}`,
      error: documentIssue(doc),
    });
    try {
      const preview = await request<{
        id: string;
        filename: string;
        parser_name?: string | null;
        content_origin?: string;
        parsed_text?: string | null;
        error_detail?: string | null;
        failure?: { message: string } | null;
      }>(`/documents/${doc.id}/preview`);
      setPreview((current) =>
        current?.id === doc.id
          ? {
              ...current,
              text: preview.parsed_text || "暂无解析内容",
              detail: `${extension(preview.filename)} · ${sizeLabel(doc.size_bytes)}${preview.content_origin === "ai_assisted" ? " · AI 辅助生成" : ""}`,
              error: preview.failure?.message || preview.error_detail || current.error,
            }
          : current,
      );
    } catch (error) {
      setPreview((current) =>
        current?.id === doc.id
          ? {
              ...current,
              text: "无法加载解析内容",
              error: error instanceof Error ? error.message : "预览加载失败",
            }
          : current,
      );
    }
  }
  const linkedDocument = params.get("document");
  const openedLinkedDocument = useRef("");
  useEffect(() => {
    if (pathname !== "/" || screen !== "knowledge" || !linkedDocument || openedLinkedDocument.current === linkedDocument) return;
    const doc = documents.find(item => item.id === linkedDocument && item.knowledge_base_id === selectedId);
    if (doc) {
      openedLinkedDocument.current = linkedDocument;
      void showDocument(doc);
    }
  }, [linkedDocument, documents, selectedId, pathname, screen]);
  function openLibrary(
    id: string,
    destination: "knowledge" | "chat" = "knowledge",
  ) {
    setSidebarOpen(false);
    const next = new URLSearchParams();
    next.set("library", id);
    next.set(destination === "knowledge" ? "view" : "chat", destination === "knowledge" ? "documents" : "1");
    router.push(`/?${next.toString()}`, { scroll: false });
  }
  const selected = libraryLoadedFor === libraryDestination && !libraryError
    ? libraries.find((item) => item.id === selectedId) : undefined;
  const activeDocuments = documents.filter((doc) => doc.status !== "deleted");
  const visibleDocuments = documents.filter(
    (doc) =>
      (doc.status === "deleted") === trash &&
      doc.filename.toLowerCase().includes(documentSearch.toLowerCase()),
  );
  const filteredLibraries = libraries.filter((item) =>
    [item.name, item.description || "", ...item.tags]
      .join(" ")
      .toLowerCase()
      .includes(librarySearch.toLowerCase()),
  );

  return (
    <>
      <WorkspaceShell screen={screen}>
      {screen === "chat" ? (
        <ChatWorkspace libraries={libraries} conversationId={params.get("conversation")} />
      ) : <>
      <WorkspaceNavigation>
        <KnowledgeNavigation
          screen={screen}
          view={view}
          selected={selected}
          libraries={libraries}
          filteredLibraries={filteredLibraries}
          activeDocumentCount={activeDocuments.length}
          librarySearch={librarySearch}
          libraryLoading={libraryLoading}
          canEdit={canEdit}
          onCatalog={() => {
            router.push("/", { scroll: false });
            setSidebarOpen(false);
          }}
          onViewChange={(nextView) => {
            setSidebarOpen(false);
            if (selectedId) {
              const params = new URLSearchParams();
              params.set("library", selectedId);
              params.set("view", nextView);
              router.push(`/?${params.toString()}`, { scroll: false });
            }
            if (nextView === "settings") void refreshHealth();
          }}
          onLibrarySearchChange={setLibrarySearch}
          onOpenLibrary={openLibrary}
          onCreateLibrary={() => openModal("create")}
        />
      </WorkspaceNavigation>
        <WorkspaceHeader title={screen === "catalog" ? "知识库" : selected?.name || "知识库"}
          context={screen === "knowledge" ? ({ documents: "文档", retrieval: "检索测试", settings: "配置" }[view]) : undefined}
          actions={screen === "catalog" ? <button className="primary" disabled={!canEdit} onClick={() => openModal("create")}>
            <Plus size={16} />新建知识库
          </button> : selected && (
            <button
              className="secondary"
              onClick={() => openLibrary(selectedId, "chat")}
            >
              <MessageSquare size={15} />
              前往问答
            </button>
          )} />
        <div className="workspace-body">
        {libraryError && <div className="workspace-request-error" role="alert">
          <CircleAlert size={16} /><span>{libraryError}</span>
          <button className="secondary" onClick={() => void loadLibraries().catch(notifyError)}><RefreshCw size={15} />重试</button>
        </div>}
        {screen === "catalog" ? (
          <Motion.section
            className="catalog"
            aria-label="知识库列表"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
          >
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
            {libraryLoading && <WorkspaceSkeleton />}
            <Motion.div layout className="library-grid">
              {filteredLibraries.map((library, index) => (
                <Motion.button
                  className="library-card"
                  key={library.id}
                  onClick={() => openLibrary(library.id)}
                  layout
                  initial={{ opacity: 0, y: 16, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  transition={{
                    duration: 0.32,
                    delay: Math.min(index, 5) * 0.045,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                  whileHover={{ y: -4, scale: 1.01 }}
                  whileTap={{ scale: 0.99 }}
                >
                  <span className="catalog-library-icon">
                    <BookOpen size={23} strokeWidth={1.7} />
                  </span>
                  <div className="library-card-title">
                    <h2>{library.name}</h2>
                    <span className={`library-status ${library.status}`}>
                      {LIBRARY_STATUS[library.status] || library.status}
                    </span>
                  </div>
                  <p>{library.description || "暂无描述"}</p>
                  {library.tags.length > 0 && (
                    <div className="library-card-tags">
                      {library.tags.map((tag) => (
                        <span key={tag}>{tag}</span>
                      ))}
                    </div>
                  )}
                  <span className="library-card-footer">
                    <span>
                      <FileText size={14} />
                      {library.document_count} 份文档
                    </span>
                    <ChevronRight size={16} />
                  </span>
                </Motion.button>
              ))}
            </Motion.div>
            {!libraryLoading && !libraryError && !filteredLibraries.length && (
              <Motion.div
                className="empty-state"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
              >
                <BookOpen size={30} />
                <h2>
                  {librarySearch
                      ? "没有匹配的知识库"
                      : "暂无知识库"}
                </h2>
              </Motion.div>
            )}
          </Motion.section>
        ) : selected ? (
          <>
            <Motion.div
              className="page-heading"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
            >
              <div className="library-symbol">
                <BookOpen size={25} strokeWidth={1.6} />
              </div>
              <div>
                <div className="title-line">
                  <span className={`subtle-label library-status ${selected.status}`}>
                    {LIBRARY_STATUS[selected.status] || selected.status}
                  </span>
                </div>
                <p>
                  {selected.description || `${selected.document_count} 份文档`}
                </p>
              </div>
            </Motion.div>
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
                  <div>
                    <dt>标签</dt>
                    <dd>
                      {selected.tags.length ? (
                        <span className="configuration-tags">
                          {selected.tags.map((tag) => (
                            <span key={tag}>{tag}</span>
                          ))}
                        </span>
                      ) : (
                        "暂无标签"
                      )}
                    </dd>
                  </div>
                </dl>
                {canEdit && (
                  <div className="configuration-actions">
                    <button className="secondary" onClick={openEditLibrary}>
                      <Settings2 size={15} />
                      编辑知识库
                    </button>
                    {selected.status === "archived" ? (
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => void changeLibraryArchive("restore")}
                      >
                        <RotateCcw size={15} />
                        恢复知识库
                      </button>
                    ) : (
                      <button
                        className="secondary danger-action"
                        disabled={busy}
                        onClick={() => {
                          if (window.confirm("归档后将无法上传、检索或开始新问答，确定继续吗？"))
                            void changeLibraryArchive("archive");
                        }}
                      >
                        <Archive size={15} />
                        归档知识库
                      </button>
                    )}
                  </div>
                )}
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
              </section>
            ) : screen === "knowledge" && view === "retrieval" ? (
              <RetrievalPanel
                key={selectedId}
                knowledgeBaseId={selectedId}
                onPreview={setPreview}
              />
            ) : (
              <Motion.section
                className="documents-view"
                id="documents-view"
                aria-label="文档"
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
              >
                {documentError && <div className="workspace-request-error" role="alert">
                  <CircleAlert size={16} /><span>{documentError}</span>
                  <button className="secondary" onClick={() => void loadDocuments(selectedId).catch(notifyError)}><RefreshCw size={15} />重试</button>
                </div>}
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
                      disabled={!canEdit || selected.status === "archived"}
                      data-upload-trigger
                      onClick={() => openModal("upload")}
                    >
                      <Plus size={16} />
                      上传文档
                    </button>
                  </div>
                </div>
                <div className="table-wrap document-table">
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
                        : visibleDocuments.map((doc) => {
                          const issue = documentIssue(doc);
                          return (
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
                                  title={issue}
                                  role="status"
                                >
                                  <span className="status-dot" />
                                  {documentRetryLabel(doc.status, doc.job?.state, doc.job?.retry_after, doc.job?.retry_progress)
                                    || STATUS[doc.status] || doc.status}
                                </span>
                                {issue && <small className="document-error">{issue}</small>}
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
                                    data-document-preview={doc.id}
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
                                        ["failed", "cancelled"].includes(doc.job.state) &&
                                        doc.job.failure?.code !== "needs_reconciliation" &&
                                        (doc.job.state === "cancelled" || doc.job.failure?.retryable !== false) && (
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
                                      {doc.personal_file_id && <a className="icon-button" href={`/resources?file=${doc.personal_file_id}`} title="在个人文件中查看" aria-label={`在个人文件中查看 ${doc.filename}`}><FolderClosed size={16} /></a>}
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
                          );
                        })}
                    </tbody>
                  </table>
                  {!loading && !documentError && !visibleDocuments.length && (
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
                          data-upload-trigger
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
              </Motion.section>
            )}
          </>
        ) : libraryLoading ? <WorkspaceSkeleton /> : !libraryError && (
          <div className="workspace-empty">
            <Layers3 size={36} strokeWidth={1.4} />
            <h2>无法访问这个知识库</h2>
            <p>知识库可能已删除，或你没有访问权限。</p>
              <button
                className="secondary"
                onClick={() => router.replace("/", { scroll: false })}
              >
                查看全部知识库
              </button>
          </div>
        )}
        </div>
      </>}
      </WorkspaceShell>
      <AnimatePresence initial={false}>
        {notice && (
          <Motion.div
            className={`toast ${notice.error ? "error" : ""}`}
            role={notice.error ? "alert" : "status"}
            initial={{ opacity: 0, x: "-50%", y: 18, scale: 0.96 }}
            animate={{ opacity: 1, x: "-50%", y: 0, scale: 1 }}
            exit={{ opacity: 0, x: "-50%", y: 10, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
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
          </Motion.div>
        )}
      </AnimatePresence>
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
            <label>
              标签<span className="optional">选填，用逗号分隔</span>
              <input
                maxLength={500}
                value={tagsInput}
                onChange={(event) => setTagsInput(event.target.value)}
                placeholder="例如：产品、技术、内部"
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
      {modal === "edit" && (
        <Modal title="编辑知识库" onClose={() => setModal(null)} busy={busy}>
          <form className="modal-form" onSubmit={updateLibrary}>
            <label>
              名称
              <input
                autoFocus
                required
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label>
              描述<span className="optional">选填</span>
              <textarea
                rows={3}
                maxLength={2000}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </label>
            <label>
              标签<span className="optional">选填，用逗号分隔</span>
              <input
                maxLength={500}
                value={tagsInput}
                onChange={(event) => setTagsInput(event.target.value)}
                placeholder="例如：产品、技术、内部"
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
                {busy && <Loader2 size={15} className="spin" />}保存修改
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
      {sourcePreview && <FilePreview key={`${session.user.id}:${sourcePreview.id}`} file={{ type: "document", id: sourcePreview.id, name: sourcePreview.filename }}
        restoreFocusSelector={`[data-document-preview="${sourcePreview.id}"]`}
        scope={session.user.id} onClose={() => setSourcePreview(null)}
        alternate={<div className="file-viewer-info"><button type="button" className="secondary" onClick={() => { const doc = sourcePreview; setSourcePreview(null); void showParsedDocument(doc); }}>查看解析文本</button></div>} />}
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
    </>
  );
}
