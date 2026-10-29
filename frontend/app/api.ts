import { createParser } from "eventsource-parser";

const configuredApiUrl =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 15_000;

// Keep same-host deployments on the current page protocol even if a stale
// build-time environment value still contains http://.
export const API_URL =
  typeof window !== "undefined" &&
  window.location.protocol === "https:" &&
  configuredApiUrl.startsWith("http://") &&
  new URL(configuredApiUrl).hostname === window.location.hostname
    ? configuredApiUrl.replace(/^http:\/\//, "https://")
    : configuredApiUrl;

let csrfToken = "";
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export function setCsrfToken(value: string) {
  csrfToken = value;
}
export function sessionHeaders(options?: RequestInit) {
  const headers = new Headers(options?.headers);
  headers.set("X-RAG-Request", "1");
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  return headers;
}

export async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = path.startsWith("/site/admin/storage") || /\/thinking\/test$/.test(path)
    || /^\/uploads\/[^/]+\/complete$/.test(path) ? 120_000
    : options?.body instanceof FormData && /^\/knowledge-bases\/[^/]+\/documents$/.test(path)
    ? 30 * 60_000
    : path.startsWith("/auth/") || path.startsWith("/site/settings/auth") || path.endsWith("/resend-verification")
      ? 60_000 : REQUEST_TIMEOUT_MS;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);
  const upstreamSignal = options?.signal;
  const abortFromCaller = () => controller.abort();
  upstreamSignal?.addEventListener("abort", abortFromCaller, { once: true });
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...options,
      credentials: "include",
      headers: sessionHeaders(options),
      signal: controller.signal,
    });
  } catch (error) {
    if (timedOut) throw new Error("连接服务超时，请检查服务状态后重试");
    throw error;
  } finally {
    window.clearTimeout(timer);
    upstreamSignal?.removeEventListener("abort", abortFromCaller);
  }
  if (response.status === 401 && !["/auth/me", "/auth/login", "/auth/register", "/auth/bootstrap"].includes(path))
    window.dispatchEvent(new Event("rag-session-expired"));
  if (response.status === 204) return undefined as T;
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(response.status,
      typeof data.detail === "string"
        ? data.detail
        : `请求失败 (${response.status})`,
    );
  return data as T;
}

export async function downloadFile(path: string, filename: string): Promise<void> {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: "include", headers: sessionHeaders(),
  });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("rag-session-expired"));
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `下载失败 (${response.status})`);
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function streamRequest(
  path: string,
  body: unknown,
  signal: AbortSignal,
  onEvent: (name: string, data: unknown) => void,
) {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    method: "POST",
    headers: sessionHeaders({
      headers: { "Content-Type": "application/json" },
    }),
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("rag-session-expired"));
    const error = await response.json().catch(() => ({}));
    throw new ApiError(response.status,
      typeof error.detail === "string"
        ? error.detail
        : `请求失败 (${response.status})`,
    );
  }
  if (!response.body) throw new Error("浏览器未提供回答数据流");
  let done = false;
  const eventIds = new Set<string>();
  const parser = createParser({
    onEvent(event) {
      if (event.id && eventIds.has(event.id)) return;
      if (event.id) eventIds.add(event.id);
      const data: unknown = JSON.parse(event.data);
      if (event.event === "done") done = true;
      onEvent(event.event || "message", data);
    },
    onError(error) {
      throw error;
    },
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    while (true) {
      const { value, done: ended } = await reader.read();
      if (ended) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    if (!done && !signal.aborted) throw new Error("回答连接已中断，可以重试");
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
