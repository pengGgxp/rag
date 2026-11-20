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
  constructor(public status: number, message: string, public detail?: unknown) { super(message); }
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
    || /^\/workspaces\/[^/]+\/files/.test(path)
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
      errorMessage(data.detail, response.status),
      data.detail,
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
  let response = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    method: body === undefined ? "GET" : "POST",
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
      errorMessage(error.detail, response.status),
    );
  }
  let eventPath = body === undefined ? path : "";
  if (response.headers.get("content-type")?.includes("application/json")) {
    const task = await response.json() as { request_id: string };
    onEvent("submitted", task);
    eventPath = `${path.replace(/\/(messages|compact)$/, "")}/requests/${encodeURIComponent(task.request_id)}/events`;
    response = await fetch(`${API_URL}${eventPath}`, { credentials: "include", headers: sessionHeaders(), signal });
  }
  if (!response.ok || !response.body) throw new Error("连接暂时不可用，回答会继续处理，请重新打开对话");
  let done = false;
  let after = 0;
  const eventIds = new Set<string>();
  const parser = createParser({
    onEvent(event) {
      if (event.id && eventIds.has(event.id)) return;
      if (event.id) eventIds.add(event.id);
      if (event.id) after = Number(event.id.split(":").at(-1)) || after;
      const data: unknown = JSON.parse(event.data);
      if (event.event === "done") done = true;
      onEvent(event.event || "message", data);
    },
    onError(error) {
      throw error;
    },
  });
  for (let attempt = 0; !done; attempt++) {
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    try {
      while (true) {
        const { value, done: ended } = await reader.read();
        if (ended) break;
        parser.feed(decoder.decode(value, { stream: true }));
      }
      parser.feed(decoder.decode());
    } catch (error) {
      if (signal.aborted || !eventPath || attempt >= 3) throw error;
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    if (done || signal.aborted) return;
    if (!eventPath || attempt >= 3) throw new Error("连接已中断，回答会继续处理，请重新打开对话");
    parser.reset();
    await new Promise(resolve => setTimeout(resolve, 600 * (attempt + 1)));
    response = await fetch(`${API_URL}${eventPath}?after=${after}`, { credentials: "include", headers: sessionHeaders(), signal });
    if (!response.ok || !response.body) throw new Error("无法恢复连接，请重新打开对话");
  }
}
