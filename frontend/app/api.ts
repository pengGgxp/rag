import { createParser } from "eventsource-parser";

const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL;
if (!configuredApiUrl) {
  throw new Error("NEXT_PUBLIC_API_URL must be set at build time");
}
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
  public status: number;
  public detail?: unknown;
  public retryAfter?: number;
  public requestId?: string;

  constructor(status: number, message: string, detail?: unknown, retryAfter?: number, requestId?: string) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
    this.requestId = requestId;
  }
}

export type DocumentFailure = {
  code: string;
  stage: string;
  message: string;
  retryable: boolean;
  request_id: string;
};

export function retryAfterMs(value: string | null): number | undefined {
  if (!value) return;
  if (/^\d+$/.test(value)) return Number(value) * 1_000;
  const date = Date.parse(value);
  if (Number.isFinite(date)) return Math.max(0, date - Date.now());
}

export function isTemporary(error: unknown) {
  if (!(error instanceof ApiError)) return false;
  if (error.detail && typeof error.detail === "object" && "retryable" in error.detail)
    return error.detail.retryable === true;
  return error.status === 0 || error.status === 429 || error.status >= 500;
}

type DocumentRoute = { route: string; libraryId?: string; uploadId?: string; jobId?: string; documentId?: string };
function documentRoute(path: string): DocumentRoute | undefined {
  const pathname = path.split("?", 1)[0];
  let match = pathname.match(/^\/knowledge-bases\/([^/]+)\/uploads$/);
  if (match) return { route: "/knowledge-bases/{library_id}/uploads", libraryId: match[1] };
  match = pathname.match(/^\/knowledge-bases\/([^/]+)\/documents$/);
  if (match) return { route: "/knowledge-bases/{library_id}/documents", libraryId: match[1] };
  match = pathname.match(/^\/uploads\/([^/]+)(\/parts\/(\d+)|\/complete)?$/);
  if (match) return {
    route: match[2]?.startsWith("/parts/") ? "/uploads/{upload_id}/parts/{number}"
      : match[2] ? "/uploads/{upload_id}/complete" : "/uploads/{upload_id}",
    uploadId: match[1],
  };
  match = pathname.match(/^\/jobs\/([^/]+)(\/retry|\/cancel)?$/);
  if (match) return {
    route: match[2] ? `/jobs/{job_id}${match[2]}` : "/jobs/{job_id}",
    jobId: match[1],
  };
  match = pathname.match(/^\/documents\/([^/]+)\/reindex$/);
  if (match) return { route: "/documents/{document_id}/reindex", documentId: match[1] };
  return undefined;
}

function documentStage(path: string): string {
  const route = documentRoute(path)?.route;
  if (!route) return "response";
  if (route === "/uploads/{upload_id}" && path.split("?", 1)[0].startsWith("/uploads/")) return "query";
  if (route.endsWith("/complete")) return "verification";
  if (route.includes("/parts/") || route.endsWith("/uploads")) return "upload";
  return "processing";
}

function newRequestId() {
  return globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function documentFailure(
  path: string,
  code: string,
  message: string,
  retryable: boolean,
  requestId = newRequestId(),
): DocumentFailure {
  return {
    code,
    stage: documentStage(path),
    message,
    retryable,
    request_id: requestId,
  };
}

function failureCode(detail: unknown) {
  return detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string"
    ? detail.code : undefined;
}

export function logDocumentFailure(
  path: string,
  diagnostic: { status: number; contentType?: string | null; requestId?: string; code?: string },
) {
  const route = documentRoute(path);
  if (!route) return;
  console.warn("document_request_failed", {
    route: route.route,
    status: diagnostic.status,
    content_type: diagnostic.contentType || undefined,
    request_id: diagnostic.requestId,
    library_id: route.libraryId,
    upload_id: route.uploadId,
    job_id: route.jobId,
    document_id: route.documentId,
    code: diagnostic.code,
  });
}

const VALIDATION_FIELD_NAMES: Record<string, string> = {
  administrator_contact: "管理员联系方式",
  version: "配置版本", provider_key: "支付渠道", enabled: "启用状态",
  api_base_url: "支付网关地址", merchant_num: "商户号", pay_type: "支付方式编码",
  secret: "商户密钥", private_key: "商户 RSA 私钥", public_key: "平台 RSA 公钥",
  packages: "充值档位", id: "档位 ID", name: "显示名称", amount_fen: "金额", credits: "到账积分",
  package_id: "充值档位", quote: "充值报价", payment_method_id: "付款方式", custom_amount_fen: "充值金额",
  payment_methods: "付款方式", code: "网关编码", label: "显示名称", custom_credits_per_yuan: "每元积分数",
};

function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((entry) => {
      if (!entry || typeof entry !== "object") return [];
      const error = entry as { loc?: unknown[]; msg?: unknown };
      if (typeof error.msg !== "string") return [];
      const field = Array.isArray(error.loc)
        ? [...error.loc].reverse().find((part): part is string => typeof part === "string" && !["body", "query", "path"].includes(part))
        : undefined;
      return [`${field ? VALIDATION_FIELD_NAMES[field] ?? "配置" : "配置"}：${error.msg}`];
    });
    if (messages.length) return [...new Set(messages)].join("；");
  }
  return `请求失败 (${status})`;
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
  const skillImport = path === "/site/admin/skills/import"
    || path === "/agent/skills/import" || path === "/agent/skills/import-folder";
  const skillPackageProcessing = /^\/(?:agent\/skills|site\/admin\/skills)\/uploads\/[^/]+\/(?:preview|import)$/.test(path);
  const timeout = skillImport || skillPackageProcessing
    ? 30 * 60_000
    : path.startsWith("/site/admin/storage") || /\/thinking\/test$/.test(path)
    || /\/models\/discover$/.test(path) || path === "/site/admin/search/test"
    || path === "/chat/attachments" || (path === "/resources" && options?.body instanceof FormData)
    || path.startsWith("/site/admin/skills")
    || path.startsWith("/agent/")
    || path.startsWith("/resources/operations/")
    || path.startsWith("/resources/browse") || path.startsWith("/resources/directories")
    || path === "/files" || path.startsWith("/files/")
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
  if (upstreamSignal?.aborted) abortFromCaller();
  let diagnosticStatus = 0;
  let diagnosticContentType: string | null = null;
  let diagnosticRequestId: string | undefined;
  let diagnosticDetail: unknown;
  try {
    let response: Response;
    try {
      response = await fetch(`${API_URL}${path}`, {
        ...options,
        credentials: "include",
        headers: sessionHeaders(options),
        signal: controller.signal,
      });
    } catch (error) {
      const message = timedOut ? "连接服务超时，请稍后重试" : "无法连接服务，请检查网络后重试";
      const detail = documentFailure(path, timedOut ? "request_timeout" : "network_error", message, true);
      if (timedOut) throw new ApiError(0, message, detail, undefined, detail.request_id);
      if (upstreamSignal?.aborted) throw error;
      throw new ApiError(0, message, detail, undefined, detail.request_id);
    }
    diagnosticStatus = response.status;
    diagnosticContentType = response.headers.get("content-type");
    diagnosticRequestId = response.headers.get("x-request-id") || undefined;
    if (response.status === 401 && !["/auth/me", "/auth/login", "/auth/register", "/auth/bootstrap"].includes(path))
      window.dispatchEvent(new Event("rag-session-expired"));
    if (response.status === 204) return undefined as T;
    const mediaType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
    const retryAfter = retryAfterMs(response.headers.get("retry-after"));
    let data: unknown;
    let parsed = false;
    if (mediaType === "application/json" || mediaType?.endsWith("+json")) {
      let body: string;
      try { body = await response.text(); }
      catch (error) {
        if (upstreamSignal?.aborted) throw error;
        const message = timedOut ? "读取服务响应超时，请稍后重试" : "读取服务响应失败，请稍后重试";
        const retryable = response.ok || response.status === 429 || response.status >= 500;
        const detail = documentFailure(path, "transport_error", message, retryable, diagnosticRequestId);
        throw new ApiError(response.ok ? 0 : response.status, message, detail, retryAfter, detail.request_id);
      }
      try {
        data = JSON.parse(body) as unknown;
        parsed = true;
      } catch {
        // A malformed body is distinct from an interrupted response read.
      }
    }
    if (!response.ok) {
      let detail = data && typeof data === "object" && "detail" in data ? data.detail : undefined;
      diagnosticDetail = detail;
      if (detail && typeof detail === "object" && "request_id" in detail && typeof detail.request_id === "string")
        diagnosticRequestId = detail.request_id;
      const fallback = response.status >= 500 ? "服务暂时不可用，请稍后重试"
        : response.status === 429 ? "请求过于频繁，请稍后重试" : errorMessage(undefined, response.status);
      if (detail === undefined) {
        const code = mediaType === "application/json" || mediaType?.endsWith("+json")
          ? parsed ? "invalid_response" : "malformed_response"
          : response.status === 429 ? "rate_limited" : response.status >= 500 ? "service_unavailable" : "request_failed";
        detail = documentFailure(path, code, fallback, response.status === 429 || response.status >= 500, diagnosticRequestId);
        diagnosticDetail = detail;
        diagnosticRequestId = (detail as DocumentFailure).request_id;
      }
      throw new ApiError(response.status, detail === undefined ? fallback : errorMessage(detail, response.status),
        detail, retryAfter, diagnosticRequestId);
    }
    if (!parsed) throw new ApiError(response.status, "服务返回异常，请稍后重试", {
      ...documentFailure(path, "protocol_error", "服务返回异常，请稍后重试", true, diagnosticRequestId),
    }, retryAfter, diagnosticRequestId);
    return data as T;
  } catch (error) {
    if (!(upstreamSignal?.aborted && error instanceof DOMException && error.name === "AbortError")) {
      const status = error instanceof ApiError ? error.status : diagnosticStatus;
      const detail = error instanceof ApiError ? error.detail : diagnosticDetail;
      const requestId = error instanceof ApiError ? error.requestId : diagnosticRequestId;
      logDocumentFailure(path, {
        status,
        contentType: diagnosticContentType,
        requestId,
        code: failureCode(detail),
      });
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    upstreamSignal?.removeEventListener("abort", abortFromCaller);
  }
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
  const headers = sessionHeaders({ headers: { "Content-Type": "application/json" } });
  const read = (url: string) => fetch(`${API_URL}${url}`, {
    credentials: "include",
    headers,
    signal,
  });
  async function verify(response: Response) {
    if (response.ok) return response;
    if (response.status === 401)
      window.dispatchEvent(new Event("rag-session-expired"));
    const error = await response.json().catch(() => ({}));
    throw new ApiError(response.status, errorMessage(error.detail, response.status));
  }
  let eventPath = body === undefined ? path : "";
  let response: Response | undefined;
  if (body !== undefined) {
    const post = () => fetch(`${API_URL}${path}`, {
      credentials: "include", method: "POST", headers, body: JSON.stringify(body), signal,
    });
    response = await verify(await post().catch(error => { if (signal.aborted) throw error; return post(); }));
    if (response.headers.get("content-type")?.includes("application/json")) {
      const task = await response.json() as { request_id: string };
      onEvent("submitted", task);
      eventPath = `${path.replace(/\/(messages|compact)$/, "")}/requests/${encodeURIComponent(task.request_id)}/events`;
      response = undefined;
    }
  }
  let done = false;
  let after = Number(new URLSearchParams(eventPath.split("?")[1]).get("after")) || 0;
  let failures = 0;
  let reconnecting = false;
  const parser = createParser({
    onEvent(event) {
      const sequence = event.id ? Number(event.id.split(":").at(-1)) : 0;
      if (sequence > 0 && sequence <= after) return;
      const data: unknown = JSON.parse(event.data);
      if (sequence > 0) after = sequence;
      if (event.event === "done") done = true;
      onEvent(event.event || "message", data);
    },
    onError(error) {
      throw error;
    },
  });
  while (!done && !signal.aborted) {
    try {
      response ??= await verify(await read(`${eventPath.split("?")[0]}?after=${after}`));
      if (!response.body) throw new Error("连接暂时不可用");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      try {
        while (!done) {
          const { value, done: ended } = await reader.read();
          if (ended) break;
          if (reconnecting) { reconnecting = false; onEvent("browser_connection", { reconnecting }); }
          failures = 0;
          parser.feed(decoder.decode(value, { stream: true }));
        }
        if (!done) parser.feed(decoder.decode());
      } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    } catch (error) {
      if (signal.aborted || !eventPath || (error instanceof ApiError && error.status < 500 && error.status !== 429)) throw error;
    }
    if (done || signal.aborted) return;
    if (!eventPath) throw new Error("连接已中断，可以重新打开对话");
    if (!reconnecting) { reconnecting = true; onEvent("browser_connection", { reconnecting }); }
    parser.reset();
    response = undefined;
    await new Promise<void>(resolve => {
      const end = () => { clearTimeout(timer); signal.removeEventListener("abort", end); resolve(); };
      const timer = setTimeout(end, Math.min(5000, 600 * ++failures));
      signal.addEventListener("abort", end, { once: true });
      if (signal.aborted) end();
    });
  }
}
