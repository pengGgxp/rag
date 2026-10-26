"use client";

import {
  ArrowLeft,
  BookOpen,
  FolderClosed,
  Plus,
  Search,
  Settings2,
} from "lucide-react";
import type {
  KnowledgeBase,
  KnowledgeView,
  WorkspaceScreen,
} from "./workspace-types";

export function KnowledgeNavigation({
  screen,
  view,
  selected,
  libraries,
  filteredLibraries,
  activeDocumentCount,
  librarySearch,
  libraryLoading,
  canEdit,
  canManagePermissions,
  onCatalog,
  onViewChange,
  onLibrarySearchChange,
  onOpenLibrary,
  onCreateLibrary,
}: {
  screen: WorkspaceScreen;
  view: KnowledgeView;
  selected?: KnowledgeBase;
  libraries: KnowledgeBase[];
  filteredLibraries: KnowledgeBase[];
  activeDocumentCount: number;
  librarySearch: string;
  libraryLoading: boolean;
  canEdit: boolean;
  canManagePermissions: boolean;
  onCatalog: () => void;
  onViewChange: (view: KnowledgeView) => void;
  onLibrarySearchChange: (value: string) => void;
  onOpenLibrary: (id: string) => void;
  onCreateLibrary: () => void;
}) {
  return (
    <>
      {screen === "knowledge" && selected && (
        <nav className="knowledge-navigation" aria-label="知识库管理">
          <button className="back-link" onClick={onCatalog}>
            <ArrowLeft size={15} />
            全部知识库
          </button>
          <div className="knowledge-navigation-title">
            <BookOpen size={18} />
            <strong>{selected.name}</strong>
          </div>
          <button
            className={view === "documents" ? "selected" : ""}
            onClick={() => onViewChange("documents")}
            aria-current={view === "documents" ? "page" : undefined}
          >
            <FolderClosed size={16} />
            文档<span>{activeDocumentCount}</span>
          </button>
          <button
            className={view === "retrieval" ? "selected" : ""}
            disabled={selected.status === "archived"}
            onClick={() => onViewChange("retrieval")}
            aria-current={view === "retrieval" ? "page" : undefined}
          >
            <Search size={16} />
            检索测试
          </button>
          {canManagePermissions && (
            <button
              className={view === "permissions" ? "selected" : ""}
              onClick={() => onViewChange("permissions")}
              aria-current={view === "permissions" ? "page" : undefined}
            >
              <Settings2 size={16} />
              访问权限
            </button>
          )}
          <button
            className={view === "settings" ? "selected" : ""}
            onClick={() => onViewChange("settings")}
            aria-current={view === "settings" ? "page" : undefined}
          >
            <Settings2 size={16} />
            配置
          </button>
        </nav>
      )}
      {screen !== "catalog" && (
        <>
          <div className="sidebar-section-heading">
            <span>切换知识库</span>
            <span className="sidebar-heading-actions">
              <span>{libraries.length}</span>
              <button
                className="icon-button"
                title="新建知识库"
                aria-label="新建知识库"
                disabled={!canEdit}
                onClick={onCreateLibrary}
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
              onChange={(event) => onLibrarySearchChange(event.target.value)}
            />
          </label>
          <nav className="library-list" aria-label="知识库">
            {filteredLibraries.map((library) => (
              <button
                key={library.id}
                className={`library-item ${selected?.id === library.id ? "selected" : ""}`}
                aria-current={selected?.id === library.id ? "page" : undefined}
                onClick={() => onOpenLibrary(library.id)}
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
            onClick={onCreateLibrary}
          >
            <Plus size={16} />
            新建知识库
          </button>
        </>
      )}
    </>
  );
}
