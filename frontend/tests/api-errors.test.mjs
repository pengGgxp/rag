import assert from "node:assert/strict";
import test from "node:test";

process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
const events = [];
globalThis.window = {
  location: { protocol: "http:", hostname: "app.example.test" },
  setTimeout,
  clearTimeout,
  dispatchEvent: event => events.push(event.type),
};
const { ApiError, request } = await import("../app/api.ts");

test("non-JSON error responses preserve status and hide response bodies", async () => {
  for (const response of [
    new Response("private traceback", { status: 500, headers: { "content-type": "text/plain" } }),
    new Response("<html>private gateway page</html>", { status: 502, headers: { "content-type": "text/html" } }),
    new Response(null, { status: 503 }),
  ]) {
    globalThis.fetch = async () => response;
    await assert.rejects(request("/uploads/example"), error => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, response.status);
      assert.equal(error.message, "服务暂时不可用，请稍后重试");
      assert.equal(error.detail?.code, "service_unavailable");
      assert.equal(error.detail?.retryable, true);
      assert.equal(JSON.stringify(error.detail).includes("private"), false);
      return true;
    });
  }
});

test("malformed JSON errors keep the HTTP status without leaking the body", async () => {
  globalThis.fetch = async () => new Response("private", {
    status: 500, headers: { "content-type": "application/problem+json" },
  });
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 500 && !error.message.includes("private"));
});

test("response body read failures retain status and expose only safe diagnostics", async () => {
  const response = new Response("private", {
    status: 502, headers: { "content-type": "application/json" },
  });
  Object.defineProperty(response, "text", {
    value: async () => { throw new TypeError("private stream details"); },
  });
  globalThis.fetch = async () => response;
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 502
      && error.detail?.code === "transport_error"
      && error.detail?.retryable === true
      && !error.message.includes("private"));
});

test("successful non-JSON responses are protocol errors", async () => {
  globalThis.fetch = async () => new Response("unexpected", {
    status: 200, headers: { "content-type": "text/plain" },
  });
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 200 && error.detail?.code === "protocol_error");
});

test("429 honors Retry-After and 401 keeps the session-expired event", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "slow down" }), {
    status: 429, headers: { "content-type": "application/json", "retry-after": "3" },
  });
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 429 && error.retryAfter === 3000);

  globalThis.fetch = async () => new Response("not json", { status: 401 });
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 401);
  assert.deepEqual(events, ["rag-session-expired"]);
});

test("network errors become safe retryable API errors and 204 has no body", async () => {
  globalThis.fetch = async () => { throw new TypeError("private network details"); };
  await assert.rejects(request("/uploads/example"), error =>
    error instanceof ApiError && error.status === 0 && !error.message.includes("private"));

  globalThis.fetch = async () => new Response(null, { status: 204 });
  assert.equal(await request("/uploads/example"), undefined);
});

test("caller cancellation remains an AbortError", async () => {
  const controller = new AbortController();
  globalThis.fetch = async (_url, { signal }) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("caller cancelled", "AbortError")), { once: true });
  });
  const pending = request("/uploads/example", { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, error => error instanceof DOMException && error.name === "AbortError");
});
