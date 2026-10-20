import { createParser } from "eventsource-parser";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, options);
  if (response.status === 204) return undefined as T;
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `请求失败 (${response.status})`,
    );
  return data as T;
}

export async function streamRequest(
  path: string,
  body: unknown,
  signal: AbortSignal,
  onEvent: (name: string, data: unknown) => void,
) {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : `请求失败 (${response.status})`,
    );
  }
  if (!response.body) throw new Error("浏览器未提供回答数据流");
  let done = false;
  const parser = createParser({
    onEvent(event) {
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
