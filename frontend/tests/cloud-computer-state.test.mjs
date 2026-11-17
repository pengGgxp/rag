import test from "node:test";
import assert from "node:assert/strict";
import { CloudComputerStore, previewReady } from "../app/cloud-computer-state.ts";
import { formatCredits } from "../app/credit-format.ts";

const computer = (id = "a", patch = {}) => ({
  id, computer_id: id, name: `任务 ${id}`, conversation_id: "original-chat",
  status: "ready", summary: "云电脑运行中", terminal: false, credits: "4.9765",
  credits_per_minute: "2.1234", seconds: 149, idle_ends_at: null, max_ends_at: null, ...patch,
});
const preview = id => ({ id: `preview-${id}`, computer_id: id, name: `预览 ${id}`,
  status: "ready", summary: "预览就绪", terminal: false, url: null });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function fixture(scope = {}) {
  const values = new Map(["a", "b"].map(id => [id, computer(id)]));
  const calls = [], handlers = new Map();
  let failure = null;
  const request = async (path, options) => {
    calls.push({ path, options });
    if (handlers.has(path)) return handlers.get(path)(options);
    if (failure) throw failure;
    const id = path.split("/")[2];
    if (path.startsWith("/previews/") && !options?.method) {
      const owner = id.replace("preview-", "");
      return { preview: preview(owner), computer: structuredClone(values.get(owner)), conversation_id: "original-chat" };
    }
    if (path.endsWith("/end")) {
      values.set(id, computer(id, { status: "stopping" }));
      return { computer: values.get(id) };
    }
    if (path.endsWith("/heartbeat")) return {};
    throw new Error(`Unexpected workspace-wide query: ${path}`);
  };
  const store = new CloudComputerStore({ userId: "owner", workspaceId: "w", conversationId: "new-chat", ...scope }, request);
  return { store, calls, handlers, values, fail: value => { failure = value; } };
}
async function open(f, id = "a") { f.store.selectPreview(`preview-${id}`); await f.store.refresh(); }

test("empty chat does not query catalogs, instances, commands or previews", async () => {
  const f = fixture();
  await f.store.refresh(); await f.store.refresh(true);
  assert.deepEqual(f.calls, []);
});

test("message references are checked once and closed previews are not polled or renewed", async () => {
  const f = fixture();
  const release = f.store.watchPreview("preview-a");
  await Promise.resolve(); await f.store.refresh();
  assert(previewReady(f.store.getSnapshot(), "preview-a"));
  assert.equal(formatCredits(Number(f.store.getSnapshot().computers.a.credits)), "4.9765");
  const count = f.calls.length;
  await f.store.refresh(); await f.store.renew("preview-a");
  assert.equal(f.calls.length, count);
  assert(f.calls.every(call => call.path === "/previews/preview-a" && !call.options?.method));
  release();
});

test("open preview polls only its own environment, coalesces requests and freezes server rate", async () => {
  const f = fixture();
  await open(f);
  const count = f.calls.length;
  await Promise.all([f.store.refresh(), f.store.refresh()]);
  assert.equal(f.calls.length, count + 1);
  assert.equal(f.store.getSnapshot().computers.a.credits_per_minute, "2.1234");
  assert.equal(f.store.getSnapshot().computers.b, undefined);
  assert(f.calls.every(call => call.path === "/previews/preview-a"));
  f.store.selectPreview(null);
  await f.store.refresh();
  assert.equal(f.calls.length, count + 1);
});

test("ending one preview leaves other referenced instances active and deduplicates", async () => {
  const f = fixture(); await open(f, "b"); await open(f);
  await Promise.all([f.store.end("a"), f.store.end("a")]);
  assert.equal(f.store.getSnapshot().computers.a.status, "stopping");
  assert(previewReady(f.store.getSnapshot(), "preview-b"));
  assert(!previewReady(f.store.getSnapshot(), "preview-a"));
  assert.equal(f.calls.filter(call => call.path.endsWith("/end")).length, 1);
});

test("late poll cannot overwrite terminal receipt or first stop cause", async () => {
  const f = fixture(); await open(f);
  f.values.set("a", computer("a", { status: "stopping", stop_reason: "reservation_expired" }));
  await f.store.refresh();
  f.values.set("a", computer("a")); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computers.a.stop_reason, "reservation_expired");
  f.values.set("a", computer("a", { status: "ended", terminal: true, credits: "5.0033" }));
  await f.store.refresh();
  f.values.set("a", computer("a")); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computers.a.credits, "5.0033");
  assert(f.store.getSnapshot().computers.a.terminal);
});

test("lost end response preserves last state and permits a scoped retry", async () => {
  const f = fixture(); await open(f);
  f.handlers.set("/cloud-computers/a/end", () => { throw new Error("lost"); });
  await f.store.end("a");
  assert(!f.store.getSnapshot().computers.a.terminal && !f.store.getSnapshot().ending.a);
  assert.match(f.store.getSnapshot().actionErrors.a, /尚未确认/);
  f.handlers.delete("/cloud-computers/a/end"); await f.store.end("a");
  assert.equal(f.store.getSnapshot().computers.a.status, "stopping");
});

test("selecting another preview rejects late data", async () => {
  const f = fixture(); await open(f);
  const stale = deferred();
  f.handlers.set("/previews/preview-a", () => stale.promise);
  const pending = f.store.refresh();
  await open(f, "b");
  stale.resolve({ preview: preview("a"), computer: computer("a", { credits: "999" }) });
  await pending;
  assert.equal(f.store.getSnapshot().computers.a.credits, "4.9765");
  assert(previewReady(f.store.getSnapshot(), "preview-b"));
});

test("query failure retains last known data, hides readiness, and refresh recovers", async () => {
  const f = fixture(); await open(f);
  f.fail(new Error("down")); await f.store.refresh();
  assert.match(f.store.getSnapshot().previewErrors["preview-a"], /上次结果/);
  assert.equal(f.store.getSnapshot().computers.a.terminal, false);
  assert(!previewReady(f.store.getSnapshot(), "preview-a"));
  f.fail(null); await f.store.refresh();
  assert(previewReady(f.store.getSnapshot(), "preview-a"));
});

for (const code of [401, 403]) test(`access ${code} clears data and blocks actions`, async () => {
  const f = fixture(); await open(f);
  f.fail(Object.assign(new Error("denied"), { status: code })); await f.store.refresh();
  assert(f.store.getSnapshot().revoked);
  assert.deepEqual(f.store.getSnapshot().computers, {});
  const count = f.calls.length;
  await f.store.end("a"); await f.store.refresh(); await f.store.renew("preview-a");
  assert.equal(f.calls.length, count);
});

for (const code of [404, 410]) test(`expired preview ${code} never creates an environment`, async () => {
  const f = fixture();
  f.fail(Object.assign(new Error("gone"), { status: code })); await open(f);
  assert.equal(f.store.getSnapshot().previewErrors["preview-a"], "预览已结束");
  assert(f.calls.every(call => !call.options?.method));
});

test("late heartbeat cannot revive ended instance or install fees", async () => {
  const f = fixture(); await open(f);
  const lease = deferred(); f.handlers.set("/previews/preview-a/heartbeat", () => lease.promise);
  const pending = f.store.renew("preview-a");
  f.values.set("a", computer("a", { terminal: true, status: "ended", credits: "5" }));
  await f.store.refresh();
  lease.resolve({ computer: computer("a", { credits: "999" }) }); await pending;
  assert.equal(f.store.getSnapshot().computers.a.credits, "5");
  assert(!previewReady(f.store.getSnapshot(), "preview-a"));
});

test("disposed scope ignores late query writes", async () => {
  const f = fixture(); await open(f);
  const stale = deferred(); f.handlers.set("/previews/preview-a", () => stale.promise);
  const pending = f.store.refresh(); f.store.dispose();
  stale.resolve({ preview: preview("a"), computer: computer("a") }); await pending;
  assert.deepEqual(f.store.getSnapshot().computers, {});
  assert.deepEqual(fixture({ workspaceId: "another" }).store.getSnapshot().computers, {});
});

test("late authorization errors from the previous preview do not revoke the current one", async () => {
  const f = fixture(); await open(f);
  const stale = deferred();
  f.handlers.set("/previews/preview-a", () => stale.promise);
  const pending = f.store.refresh();
  await open(f, "b");
  stale.reject(Object.assign(new Error("previous preview revoked"), { status: 403 }));
  await pending;
  assert(!f.store.getSnapshot().revoked && previewReady(f.store.getSnapshot(), "preview-b"));
});

test("late heartbeat authorization errors are ignored after switching or disposal", async () => {
  const f = fixture(); await open(f);
  const lease = deferred();
  f.handlers.set("/previews/preview-a/heartbeat", () => lease.promise);
  const pending = f.store.renew("preview-a");
  await open(f, "b");
  lease.reject(Object.assign(new Error("old lease revoked"), { status: 403 }));
  await pending;
  assert(!f.store.getSnapshot().revoked && previewReady(f.store.getSnapshot(), "preview-b"));
  const receipt = deferred();
  f.handlers.set("/cloud-computers/b/end", () => receipt.promise);
  const ending = f.store.end("b");
  f.store.dispose();
  receipt.resolve({ computer: computer("b", { status: "ended", terminal: true }) });
  await ending;
  assert.deepEqual(f.store.getSnapshot().computers, {});
});

test("confirmed terminal state clears a previously lost end response warning", async () => {
  const f = fixture(); await open(f);
  f.handlers.set("/cloud-computers/a/end", () => { throw new Error("lost"); });
  await f.store.end("a");
  assert(f.store.getSnapshot().actionErrors.a);
  f.values.set("a", computer("a", { status: "ended", terminal: true }));
  await f.store.refresh();
  assert(!f.store.getSnapshot().actionErrors.a && !f.store.getSnapshot().ending.a);
});
