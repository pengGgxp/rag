"use client";

import { useEffect, useState } from "react";
import { Download, File, RefreshCw, Search } from "lucide-react";
import { request, API_URL } from "../api";
import { WorkspaceHeader } from "../workspace-header";
import { WorkspaceShell } from "../workspace-shell";
import { useAuth } from "../auth";

type WorkspaceFile = {
  id: string;
  path: string;
  content_type: string;
  size_bytes: number;
  version: number;
  updated_at: string;
};

function humanSize(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function WorkspaceFilesPage() {
  const { session } = useAuth();
  const [items, setItems] = useState<WorkspaceFile[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const data = await request<{ items: WorkspaceFile[] }>(
        `/workspaces/${session.workspace_id}/files?q=${encodeURIComponent(query)}`,
      );
      setItems(data.items);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "文件加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, [session.workspace_id, query]);

  return (
    <WorkspaceShell screen="files">
      <WorkspaceHeader title="工作区输出" />
      <div className="workspace-body workspace-files-page">
        <div className="workspace-files-content">
          <div className="workspace-files-toolbar">
            <label className="workspace-files-search">
              <Search size={16} aria-hidden="true" />
              <span className="sr-only">搜索输出文件</span>
              <input
                aria-label="搜索输出文件"
                placeholder="搜索输出文件"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <button
              className="icon-button workspace-files-refresh"
              type="button"
              aria-label="刷新输出列表"
              title="刷新"
              onClick={() => void load()}
              disabled={loading}
            >
              <RefreshCw size={16} aria-hidden="true" />
            </button>
          </div>

          <p className="workspace-files-hint">
            这里显示 Skill 和对话生成的临时文件，系统会定期清理。
          </p>

          {error && (
            <div className="workspace-files-error" role="alert">
              <span>{error}</span>
              <button type="button" onClick={() => void load()}>
                重试
              </button>
            </div>
          )}

          <section className="workspace-files-results" aria-label="输出文件列表" aria-busy={loading}>
            {loading && <p className="workspace-files-state">正在加载输出</p>}
            {!loading && !error && !items.length && (
              <p className="workspace-files-state">暂时没有生成文件</p>
            )}
            {!loading && !error && items.length > 0 && (
              <div className="workspace-file-list">
                {items.map((item) => (
                  <div className="workspace-file-row" key={item.id}>
                    <span className="workspace-file-icon" aria-hidden="true">
                      <File size={18} />
                    </span>
                    <div className="workspace-file-copy">
                      <strong title={item.path}>{item.path}</strong>
                      <small>
                        {humanSize(item.size_bytes)} · 最近生成于 {formatDate(item.updated_at)}
                      </small>
                    </div>
                    <a
                      className="icon-button workspace-file-download"
                      href={`${API_URL}/workspaces/${session.workspace_id}/files/${item.id}/download`}
                      aria-label={`下载 ${item.path}`}
                      title="下载"
                    >
                      <Download size={16} aria-hidden="true" />
                    </a>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </WorkspaceShell>
  );
}
