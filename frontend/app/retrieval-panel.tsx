"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  BookOpen,
  ChevronRight,
  CircleAlert,
  Copy,
  FileText,
  Loader2,
  Search,
  X,
} from "lucide-react";
import { request } from "./api";
import "./retrieval.css";

type TraceResult = {
  chunk_id: string;
  filename: string;
  workspace_path: string | null;
  rank: number;
  score: number;
  lexical_rank: number | null;
  vector_rank: number | null;
  vector_distance: number | null;
};
type QueryTrace = {
  id: string;
  knowledge_base_id: string;
  query: string;
  retrieval_mode: string;
  embedding_provider: string | null;
  embedding_model: string | null;
  embedding_dimension: number | null;
  result_count: number;
  results: TraceResult[];
  created_at: string;
};
type RetrievedChunk = {
  id: string;
  document_id: string;
  ordinal: number;
  text: string;
  token_count: number;
  char_start: number;
  char_end: number;
  heading_path: string | null;
  page_number?: number | null;
};
type Retrieval = {
  trace: QueryTrace;
  results: {
    trace_id: string;
    chunk: RetrievedChunk;
    score: number;
    filename: string;
    workspace_path: string | null;
  }[];
};
type SourcePreview = {
  filename: string;
  text: string;
  detail: string;
};
const MODES: Record<string, string> = {
  hybrid: "混合检索",
  lexical: "关键词检索",
  lexical_fallback: "关键词回退",
  no_evidence: "无足够证据",
};

function RankMetrics({ result }: { result: TraceResult }) {
  return (
    <dl className="rank-metrics">
      <div>
        <dt>RRF 分数</dt>
        <dd>{result.score.toFixed(5)}</dd>
      </div>
      <div>
        <dt>关键词排名</dt>
        <dd>{result.lexical_rank ?? "—"}</dd>
      </div>
      <div>
        <dt>向量排名</dt>
        <dd>{result.vector_rank ?? "—"}</dd>
      </div>
      <div>
        <dt>向量距离</dt>
        <dd>{result.vector_distance?.toFixed(4) ?? "—"}</dd>
      </div>
    </dl>
  );
}

function TraceSummary({ trace }: { trace: QueryTrace }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  return (
    <>
      <dl className="trace-metadata">
        <div>
          <dt>检索模式</dt>
          <dd>{MODES[trace.retrieval_mode] ?? trace.retrieval_mode}</dd>
        </div>
        <div>
          <dt>向量模型</dt>
          <dd>{trace.embedding_model || "未配置"}</dd>
        </div>
        <div>
          <dt>向量维度</dt>
          <dd>{trace.embedding_dimension ?? "—"}</dd>
        </div>
        <div>
          <dt>检索时间</dt>
          <dd>{new Date(trace.created_at).toLocaleString("zh-CN")}</dd>
        </div>
      </dl>
      <div className="trace-identity">
        <span>TRACE</span>
        <code>{trace.id}</code>
        <button
          className="icon-button"
          title={copied ? "已复制" : "复制 Trace ID"}
          aria-label="复制 Trace ID"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(trace.id);
              setCopied(true);
              setCopyError(false);
            } catch {
              setCopyError(true);
            }
          }}
        >
          <Copy size={14} />
        </button>
        {copied && <span role="status">已复制</span>}
      </div>
      {copyError && (
        <p className="retrieval-error" role="alert">
          浏览器不允许访问剪贴板
        </p>
      )}
      {trace.retrieval_mode === "lexical_fallback" && (
        <p className="retrieval-warning">
          <CircleAlert size={15} />
          向量服务暂不可用，本次仅返回关键词检索结果。
        </p>
      )}
    </>
  );
}

export function TraceInspector({
  traceId,
  knowledgeBaseId,
}: {
  traceId: string;
  knowledgeBaseId: string;
}) {
  const [trace, setTrace] = useState<QueryTrace | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setTrace(null);
    setError("");
    request<QueryTrace>(`/traces/${traceId}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        if (data.knowledge_base_id !== knowledgeBaseId)
          throw new Error("检索记录不属于当前知识库");
        setTrace(data);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof Error ? reason.message : "检索记录加载失败",
          );
      });
    return () => controller.abort();
  }, [traceId, knowledgeBaseId, attempt]);
  return (
    <div className="trace-inspector">
      {error ? (
        <div className="retrieval-error" role="alert">
          {error}
          <button
            className="secondary"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : !trace ? (
        <div className="thinking" role="status">
          <Loader2 size={17} className="spin" />
          正在加载检索记录…
        </div>
      ) : (
        <>
          <p className="trace-query">{trace.query}</p>
          <TraceSummary trace={trace} />
          <h3 className="trace-results-heading">
            召回结果<span>{trace.result_count}</span>
          </h3>
          {trace.results.map((result) => (
            <div className="trace-result" key={result.chunk_id}>
              <div className="trace-result-name">
                <span className="retrieval-rank">{result.rank}</span>
                <FileText size={16} />
                <strong>{result.filename}</strong>
              </div>
              {result.workspace_path && <code className="retrieval-source-path">{result.workspace_path}</code>}
              <RankMetrics result={result} />
              <code className="chunk-id">{result.chunk_id}</code>
            </div>
          ))}
          {!trace.results.length && (
            <p className="retrieval-muted">本次检索没有召回片段</p>
          )}
        </>
      )}
    </div>
  );
}

export function RetrievalPanel({
  knowledgeBaseId,
  onPreview,
}: {
  knowledgeBaseId: string;
  onPreview: (preview: SourcePreview) => void;
}) {
  const [query, setQuery] = useState("");
  const [retrieval, setRetrieval] = useState<Retrieval | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || loading) return;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError("");
    setRetrieval(null);
    try {
      const data = await request<Retrieval>(
        `/knowledge-bases/${knowledgeBaseId}/retrieval`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: query.trim() }),
          signal: controller.signal,
        },
      );
      if (!controller.signal.aborted) setRetrieval(data);
    } catch (reason) {
      if (!controller.signal.aborted)
        setError(reason instanceof Error ? reason.message : "检索失败，请重试");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  return (
    <section
      className="retrieval-view"
      id="retrieval-view"
      aria-label="检索测试"
    >
      <form className="retrieval-search" onSubmit={search}>
        <label htmlFor="retrieval-query">检索问题</label>
        <div className="retrieval-search-row">
          <input
            id="retrieval-query"
            maxLength={4000}
            value={query}
            placeholder="输入需要检索的问题或关键词"
            onChange={(event) => setQuery(event.target.value)}
            disabled={loading}
          />
          {loading ? (
            <button
              key="cancel"
              className="secondary"
              type="button"
              onClick={(event) => {
                event.preventDefault();
                pending.current?.abort();
                setLoading(false);
              }}
            >
              <X size={15} />
              取消
            </button>
          ) : (
            <button
              key="search"
              className="primary"
              type="submit"
              disabled={!query.trim()}
            >
              <Search size={15} />
              检索
            </button>
          )}
        </div>
      </form>
      {error && (
        <p className="retrieval-error" role="alert">
          <CircleAlert size={16} />
          {error}
        </p>
      )}
      {loading && (
        <div className="retrieval-loading" role="status">
          <Loader2 size={20} className="spin" />
          正在检索知识库…
        </div>
      )}
      {!loading && retrieval && (
        <>
          <div className="retrieval-heading">
            <h2>
              召回结果<span>{retrieval.trace.result_count}</span>
            </h2>
            <span className="retrieval-mode">
              {MODES[retrieval.trace.retrieval_mode] ??
                retrieval.trace.retrieval_mode}
            </span>
          </div>
          <details className="trace-details">
            <summary>
              检索记录
              <ChevronRight size={15} />
            </summary>
            <TraceSummary trace={retrieval.trace} />
          </details>
          {!retrieval.results.length && (
            <div className="empty-state">
              <Search size={29} strokeWidth={1.4} />
              <h2>没有召回片段</h2>
            </div>
          )}
          <div className="retrieval-results">
            {retrieval.results.map((result) => {
              const rank = retrieval.trace.results.find(
                (item) => item.chunk_id === result.chunk.id,
              );
              return (
                <article className="retrieval-result" key={result.chunk.id}>
                  <div className="retrieval-result-heading">
                    <span className="retrieval-rank">{rank?.rank}</span>
                    <FileText size={18} />
                    <h3>{result.filename}</h3>
                    <button
                      className="icon-button"
                      title="查看完整片段"
                      aria-label={`查看片段 ${rank?.rank}`}
                      onClick={() =>
                        onPreview({
                          filename: result.filename,
                          text: result.chunk.text,
                          detail: `${result.chunk.page_number ? `第 ${result.chunk.page_number} 页 · ` : ""}${result.chunk.heading_path ? `${result.chunk.heading_path} · ` : ""}字符 ${result.chunk.char_start}–${result.chunk.char_end} · Chunk ${result.chunk.id}`,
                        })
                      }
                    >
                      <BookOpen size={17} />
                    </button>
                  </div>
                  <div className="retrieval-location">
                    {result.workspace_path && <span>{result.workspace_path}</span>}
                    <span>片段 {result.chunk.ordinal + 1}</span>
                    {result.chunk.page_number && <span>第 {result.chunk.page_number} 页</span>}
                    <span>
                      字符 {result.chunk.char_start}–{result.chunk.char_end}
                    </span>
                    {result.chunk.heading_path && (
                      <span>{result.chunk.heading_path}</span>
                    )}
                  </div>
                  <p className="retrieval-excerpt">{result.chunk.text}</p>
                  {rank && (
                    <details className="result-details">
                      <summary>排名详情</summary>
                      <RankMetrics result={rank} />
                    </details>
                  )}
                </article>
              );
            })}
          </div>
        </>
      )}
      {!loading && !retrieval && !error && (
        <div className="empty-state retrieval-initial">
          <Search size={30} strokeWidth={1.4} />
          <h2>尚无检索记录</h2>
        </div>
      )}
    </section>
  );
}
