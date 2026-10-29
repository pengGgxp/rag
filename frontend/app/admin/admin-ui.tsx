"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { request } from "../api";

export type Page<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};
export type Credits = {
  balance: number;
  lifetime_granted: number;
  lifetime_spent: number;
};
export const number = (value: number) => value.toLocaleString("zh-CN");
export const date = (value: string) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });
export const json = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export function useAdminResource<T>(path: string | null) {
  const [state, setState] = useState<{
    path: string | null;
    data: T | null;
    error: string;
    loading: boolean;
  }>({ path, data: null, error: "", loading: true });
  const sequence = useRef(0);
  const load = useCallback(async () => {
    if (!path) return;
    const id = ++sequence.current;
    setState((value) => ({
      path,
      data: value.path === path ? value.data : null,
      error: "",
      loading: true,
    }));
    try {
      const data = await request<T>(path);
      if (id === sequence.current)
        setState({ path, data, error: "", loading: false });
    } catch (e) {
      if (id === sequence.current)
        setState((value) => ({
          ...value,
          error: e instanceof Error ? e.message : "加载失败",
          loading: false,
        }));
    }
  }, [path]);
  useEffect(() => {
    void load();
    return () => {
      sequence.current++;
    };
  }, [load]);
  return {
    data: state.path === path ? state.data : null,
    error: state.path === path ? state.error : "",
    loading: path !== null && (state.path !== path || state.loading),
    reload: load,
  };
}

export function AdminHeading({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="admin-heading">
      <h1>{title}</h1>
      {children}
    </header>
  );
}
export function AdminLoad({
  error,
  loading,
  retry,
  children,
}: {
  error: string;
  loading: boolean;
  retry: () => void;
  children: ReactNode;
}) {
  return (
    <>
      {error && (
        <div className="admin-error" role="alert">
          <span>{error}</span>
          <button className="secondary" onClick={retry}>
            <RefreshCw size={15} />
            重试
          </button>
        </div>
      )}
      {loading ? (
        <div className="admin-skeleton" role="status" aria-label="加载中">
          {Array.from({ length: 5 }, (_, i) => (
            <span className="auth-skeleton" key={i} />
          ))}
        </div>
      ) : (
        children
      )}
    </>
  );
}
export function Pagination({
  total,
  page,
  pageSize,
  change,
}: {
  total: number;
  page: number;
  pageSize: number;
  change: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="admin-pagination">
      <span>共 {number(total)} 条</span>
      <div>
        <button
          className="icon-button"
          aria-label="上一页"
          title="上一页"
          disabled={page <= 1}
          onClick={() => change(page - 1)}
        >
          <ChevronLeft size={17} />
        </button>
        <span>
          {page} / {pages}
        </span>
        <button
          className="icon-button"
          aria-label="下一页"
          title="下一页"
          disabled={page >= pages}
          onClick={() => change(page + 1)}
        >
          <ChevronRight size={17} />
        </button>
      </div>
    </div>
  );
}
export function CreditSummary({ credits }: { credits: Credits }) {
  return (
    <dl className="admin-metrics">
      <div>
        <dt>积分余额</dt>
        <dd>{number(credits.balance)}</dd>
      </div>
      <div>
        <dt>累计入账</dt>
        <dd>{number(credits.lifetime_granted)}</dd>
      </div>
      <div>
        <dt>累计扣减</dt>
        <dd>{number(credits.lifetime_spent)}</dd>
      </div>
    </dl>
  );
}
