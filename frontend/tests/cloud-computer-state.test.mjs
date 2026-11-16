import test from "node:test";
import assert from "node:assert/strict";
import { CloudComputerStore, previewReady } from "../app/cloud-computer-state.ts";
import { formatCredits } from "../app/credit-format.ts";

const computer = (patch = {}) => ({
  id: "env-a", status: "ready", summary: "云电脑运行中", terminal: false, credits: "4.9765",
  credits_per_minute: "2.1234", seconds: 149, idle_ends_at: "2026-11-16T09:10:00Z",
  max_ends_at: "2026-11-16T10:00:00Z", ...patch,
});
const preview = { id: "preview", name: "算法可视化", status: "ready", summary: "预览就绪", terminal: false, url: null };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const apiError = code => Object.assign(new Error("synthetic"), { status: code });
function fixture(scope = {}) {
  let value = computer(), previewValue = preview, failure = null;
  const calls = [];
  const handlers = new Map();
  const request = async (path, options) => {
    calls.push({ path, options });
    if (handlers.has(path)) return handlers.get(path)(options);
    if (failure) throw failure;
    if (path.endsWith("/catalog")) return { available: true, credits_per_minute: 99, idle_seconds: 600 };
    if (path.endsWith("/commands")) return { items: [] };
    if (path === "/previews/preview") return { preview: structuredClone(previewValue), computer: structuredClone(value), conversation_id: "chat-a" };
    if (path.endsWith("/preview")) return { preview: previewValue };
    if (path.endsWith("/cloud-computer")) return { computer: value && structuredClone(value) };
    if (path.endsWith("/heartbeat")) return { computer: computer({ credits: "999" }) };
    throw new Error(`Unmocked ${path}`);
  };
  const store = new CloudComputerStore({ userId: "owner", workspaceId: "workspace-a", conversationId: "chat-a", ...scope }, request);
  return { store, calls, handlers, set: patch => { value = computer(patch); }, setPreview: next => { previewValue = next; },
    fail: error => { failure = error; }, noComputer: () => { value = null; previewValue = null; } };
}

test("coalesces readers; fees are server values at frozen price, without heartbeats", async () => {
  const f = fixture();
  await Promise.all([f.store.refresh(), f.store.refresh(), f.store.refresh()]);
  assert.equal(f.calls.filter(call => call.path.endsWith("/cloud-computer")).length, 1);
  assert.equal(f.store.getSnapshot().computer.credits, "4.9765");
  assert.equal(f.store.getSnapshot().computer.credits_per_minute, "2.1234");
  assert.equal(formatCredits(Number(f.store.getSnapshot().computer.credits)), "4.9765");
  assert(previewReady(f.store.getSnapshot(), "preview"));
  assert(f.calls.every(call => !call.options?.method));
});

test("failure retains last state and can recover; never implies ended", async () => {
  const f = fixture(); await f.store.refresh();
  f.fail(new Error("network down")); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.terminal, false);
  assert.match(f.store.getSnapshot().error, /上次结果/);
  f.fail(null); f.set({ credits: "5.0001" }); await f.store.refresh();
  assert.equal(f.store.getSnapshot().error, "");
  assert.equal(f.store.getSnapshot().computer.credits, "5.0001");
});

test("late poll cannot overwrite end request; duplicate end submits once and waits for confirmation", async () => {
  const f = fixture(); await f.store.refresh();
  const stale = deferred(), stop = deferred();
  f.handlers.set("/conversations/chat-a/cloud-computer", () => stale.promise);
  const poll = f.store.refresh();
  f.handlers.set("/conversations/chat-a/cloud-computer/end", () => stop.promise);
  const ending = f.store.end();
  await f.store.end();
  assert(f.store.getSnapshot().ending);
  assert.equal(f.calls.filter(call => call.path.endsWith("/end")).length, 1);
  assert(!previewReady(f.store.getSnapshot(), "preview"));
  f.handlers.delete("/conversations/chat-a/cloud-computer");
  f.set({ status: "stopping", summary: "正在结束云电脑" });
  stop.resolve({ computer: computer({ status: "stopping" }) });
  await ending;
  stale.resolve({ computer: computer({ credits: "1" }) }); await poll;
  assert.equal(f.store.getSnapshot().computer.status, "stopping");
  assert.equal(f.store.getSnapshot().computer.terminal, false);
  f.set({ status: "ended", terminal: true, credits: "5.0033" }); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.credits, "5.0033");
  assert.equal(f.store.getSnapshot().ending, false);
  f.set({ status: "ready", credits: "6" }); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.status, "ended");
});

test("response replacement rejects older reads without cancelling independently started preview", async () => {
  const f = fixture(); await f.store.refresh();
  const stale = deferred();
  f.handlers.set("/conversations/chat-a/cloud-computer", () => stale.promise);
  const before = f.store.refresh();
  f.handlers.delete("/conversations/chat-a/cloud-computer");
  f.set({ credits: "7" }); await f.store.refresh(true);
  stale.resolve({ computer: computer({ credits: "1" }) }); await before;
  assert.equal(f.store.getSnapshot().computer.credits, "7");
  assert(previewReady(f.store.getSnapshot(), "preview"));
});

test("scope disposal rejects late writes; another user/workspace starts empty", async () => {
  const f = fixture(), waiting = deferred();
  f.handlers.set("/conversations/chat-a/cloud-computer", () => waiting.promise);
  const pending = f.store.refresh(); f.store.dispose();
  const other = fixture({ userId: "other", workspaceId: "workspace-b", conversationId: "chat-b" });
  assert.equal(other.store.getSnapshot().computer, null);
  waiting.resolve({ computer: computer() }); await pending;
  assert.equal(f.store.getSnapshot().computer, null);
  assert.equal(other.store.getSnapshot().computer, null);
});

for (const code of [401, 403, 404]) test(`access ${code} clears cached records and blocks reads/actions`, async () => {
  const f = fixture(); await f.store.refresh();
  f.fail(apiError(code)); await f.store.refresh();
  assert(f.store.getSnapshot().revoked);
  assert.equal(f.store.getSnapshot().computer, null);
  assert.deepEqual(f.store.getSnapshot().details, {});
  const count = f.calls.length;
  await f.store.end(); await f.store.refresh(); await f.store.renew("preview");
  assert.equal(f.calls.length, count);
});

test("heartbeat data never installs fees or revives ended state", async () => {
  const f = fixture(); await f.store.refresh();
  const lease = deferred(); f.handlers.set("/previews/preview/heartbeat", () => lease.promise);
  const renewing = f.store.renew("preview");
  f.set({ status: "ended", terminal: true, credits: "5" }); await f.store.refresh();
  lease.resolve({ computer: computer({ credits: "999" }) }); await renewing;
  assert.equal(f.store.getSnapshot().computer.credits, "5");
  assert(!previewReady(f.store.getSnapshot(), "preview"));
});

test("preview must be server-ready and belong to same environment and conversation", async () => {
  const f = fixture(); f.setPreview({ ...preview, status: "starting" }); await f.store.refresh();
  assert(!previewReady(f.store.getSnapshot(), "preview"));
  f.handlers.set("/previews/preview", () => ({ preview, computer: computer({ id: "older" }), conversation_id: "chat-a" }));
  await f.store.refresh();
  assert(!previewReady(f.store.getSnapshot(), "preview"));
  f.handlers.set("/previews/preview", () => ({ preview, computer: computer(), conversation_id: "other-chat" }));
  await f.store.refresh();
  assert.equal(f.store.getSnapshot().details.preview, undefined);
  assert.match(f.store.getSnapshot().previewErrors.preview, /不属于/);
});

test("failed end allows retry without falsely settling or clearing known data", async () => {
  const f = fixture(); await f.store.refresh();
  f.handlers.set("/conversations/chat-a/cloud-computer/end", () => { throw new Error("lost response"); });
  await f.store.end();
  assert(!f.store.getSnapshot().computer.terminal);
  assert(!f.store.getSnapshot().ending);
  assert.match(f.store.getSnapshot().actionError, /尚未确认/);
});

test("stopping preserves server reason across polls and delayed ready responses", async () => {
  const f = fixture();
  f.set({ status: "stopping", summary: "积分不足，云电脑正在结束" });
  await f.store.refresh(); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.summary, "积分不足，云电脑正在结束");
  f.set({ status: "ready", summary: "云电脑运行中" }); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.status, "stopping");
  assert.equal(f.store.getSnapshot().computer.summary, "积分不足，云电脑正在结束");
});

test("a delayed end acknowledgement cannot replace a newly created environment", async () => {
  const f = fixture(); await f.store.refresh();
  const stop = deferred(); f.handlers.set("/conversations/chat-a/cloud-computer/end", () => stop.promise);
  const ending = f.store.end();
  f.set({ id: "new-env", credits: "0" }); await f.store.refresh();
  stop.resolve({ computer: computer({ status: "stopping" }) }); await ending;
  assert.equal(f.store.getSnapshot().computer.id, "new-env");
  assert(!f.store.getSnapshot().ending);
  f.set({ id: "env-a" }); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computer.id, "new-env");
});

test("command cancel keeps preview active and uses existing command endpoint", async () => {
  const f = fixture();
  f.handlers.set("/conversations/chat-a/cloud-computer/commands", () => ({ items: [
    { id: "cmd", status: "running", terminal: false, cancel_requested: false, result: {} },
  ] }));
  f.handlers.set("/cloud-computer/commands/cmd/cancel", () => ({}));
  await f.store.refresh(); await f.store.cancel("cmd");
  assert(previewReady(f.store.getSnapshot(), "preview"));
  assert.equal(f.calls.filter(call => call.path.endsWith("/end")).length, 0);
});

test("unstarted conversation does not create an environment; standalone discovers its conversation", async () => {
  const fresh = fixture({ conversationId: null }); fresh.noComputer(); await fresh.store.refresh();
  assert.equal(fresh.store.getSnapshot().computer, null);
  assert.equal(fresh.calls.length, 1);
  const standalone = fixture({ conversationId: null, previewId: "preview" }); await standalone.store.refresh();
  assert(previewReady(standalone.store.getSnapshot(), "preview"));
  assert(standalone.calls.some(call => call.path === "/conversations/chat-a/cloud-computer"));
});
