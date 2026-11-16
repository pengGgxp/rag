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
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function fixture(scope = {}) {
  const values = new Map(["a", "b"].map(id => [id, computer(id)]));
  const calls = [], handlers = new Map();
  let failure = null;
  const request = async (path, options) => {
    calls.push({ path, options });
    if (handlers.has(path)) return handlers.get(path)(options);
    if (failure) throw failure;
    if (path.endsWith("/catalog")) return { available: true, credits_per_minute: 99 };
    if (path.startsWith("/cloud-computers?")) return { items: [...values.values()].map(value => structuredClone(value)), next_offset: null };
    const id = path.split("/")[2];
    if (path.endsWith("/commands")) return { items: [] };
    if (path.endsWith("/preview")) return { preview: preview(id) };
    if (path.startsWith("/previews/") && !options?.method) {
      const owner = id.replace("preview-", "");
      return { preview: preview(owner), computer: structuredClone(values.get(owner)), conversation_id: "original-chat" };
    }
    if (path.endsWith("/end")) {
      values.set(id, computer(id, { status: "stopping" }));
      return { computer: values.get(id) };
    }
    if (path.endsWith("/heartbeat") || path.endsWith("/cancel")) return {};
    throw new Error(`Unmocked ${path}`);
  };
  return { store: new CloudComputerStore({ userId: "owner", workspaceId: "w", conversationId: "new-chat", ...scope }, request),
    calls, handlers, values, fail: value => { failure = value; } };
}

test("coalesces polls, preserves frozen server fees and associates each preview", async () => {
  const f = fixture();
  await Promise.all([f.store.refresh(), f.store.refresh()]);
  const state = f.store.getSnapshot();
  assert.equal(f.calls.filter(call => call.path === "/cloud-computer/catalog").length, 1);
  assert.equal(formatCredits(Number(state.computers.a.credits)), "4.9765");
  assert.equal(state.computers.a.credits_per_minute, "2.1234");
  assert(previewReady(state, "preview-a") && previewReady(state, "preview-b"));
  assert(f.calls.every(call => !call.options?.method && !call.path.includes("/conversations/")));
});

test("ending one ID leaves the other instance and its preview active", async () => {
  const f = fixture(); await f.store.refresh();
  await f.store.end("a");
  const state = f.store.getSnapshot();
  assert.equal(state.computers.a.status, "stopping");
  assert.equal(state.computers.b.status, "ready");
  assert(!previewReady(state, "preview-a") && previewReady(state, "preview-b"));
  assert.equal(f.calls.filter(call => call.path.endsWith("/end")).length, 1);
  await f.store.end("a");
  assert.equal(f.calls.filter(call => call.path.endsWith("/end")).length, 1);
});

test("late poll cannot overwrite terminal receipt or first stop cause", async () => {
  const f = fixture(); await f.store.refresh();
  f.values.set("a", computer("a", { status: "stopping", stop_reason: "insufficient_balance",
    stop_reason_text: "积分不足", renewal_required_credits: "20" }));
  await f.store.refresh();
  f.values.set("a", computer("a")); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computers.a.stop_reason, "insufficient_balance");
  f.values.set("a", computer("a", { status: "ended", terminal: true, credits: "5.0033" }));
  await f.store.refresh();
  f.values.set("a", computer("a")); await f.store.refresh();
  assert.equal(f.store.getSnapshot().computers.a.credits, "5.0033");
  assert(f.store.getSnapshot().computers.a.terminal);
});

test("lost end response preserves state and permits retry only for that ID", async () => {
  const f = fixture(); await f.store.refresh();
  f.handlers.set("/cloud-computers/a/end", () => { throw new Error("lost"); });
  await f.store.end("a");
  const state = f.store.getSnapshot();
  assert(!state.computers.a.terminal && !state.ending.a);
  assert.match(state.actionErrors.a, /尚未确认/);
  assert.equal(state.actionErrors.b, undefined);
  assert(previewReady(state, "preview-b"));
});

test("replacement poll discards stale data, while end and new instance remain separate", async () => {
  const f = fixture(); await f.store.refresh();
  const stale = deferred(), stopping = deferred();
  f.handlers.set("/cloud-computers?limit=20", () => stale.promise);
  const pending = f.store.refresh();
  f.handlers.delete("/cloud-computers?limit=20");
  f.handlers.set("/cloud-computers/a/end", () => stopping.promise);
  const ending = f.store.end("a");
  f.values.set("c", computer("c")); await f.store.refresh(true);
  stopping.resolve({ computer: computer("a", { status: "stopping" }) }); await ending;
  stale.resolve({ items: [computer("a")], next_offset: null }); await pending;
  assert.equal(f.store.getSnapshot().computers.a.status, "stopping");
  assert.equal(f.store.getSnapshot().computers.c.status, "ready");
});

test("active pagination loads beyond recent history without an instance cap", async () => {
  const f = fixture();
  f.handlers.set("/cloud-computers?active_only=true&offset=0", () => ({ items: [computer("a")], next_offset: 1 }));
  f.handlers.set("/cloud-computers?active_only=true&offset=1", () => ({ items: [computer("c")], next_offset: null }));
  f.values.set("c", computer("c"));
  await f.store.refresh();
  assert.deepEqual(new Set(f.store.getSnapshot().computerIds), new Set(["a", "b", "c"]));
});

test("one instance query failure does not hide other live instances", async () => {
  const f = fixture(); await f.store.refresh();
  f.handlers.set("/cloud-computers/a/commands", () => { throw new Error("temporary"); });
  await f.store.refresh();
  assert.match(f.store.getSnapshot().instanceErrors.a, /更新失败/);
  assert(previewReady(f.store.getSnapshot(), "preview-b"));
  assert(!previewReady(f.store.getSnapshot(), "preview-a"));
});

test("global query failure retains previous results and recovers", async () => {
  const f = fixture(); await f.store.refresh();
  f.fail(new Error("down")); await f.store.refresh();
  assert.match(f.store.getSnapshot().error, /上次结果/);
  assert.equal(f.store.getSnapshot().computers.a.terminal, false);
  f.fail(null); await f.store.refresh();
  assert.equal(f.store.getSnapshot().error, "");
});

for (const code of [401, 403]) test(`access ${code} clears data and blocks actions`, async () => {
  const f = fixture(); await f.store.refresh();
  f.fail(Object.assign(new Error("denied"), { status: code })); await f.store.refresh();
  assert(f.store.getSnapshot().revoked);
  assert.deepEqual(f.store.getSnapshot().computers, {});
  const count = f.calls.length;
  await f.store.end("a"); await f.store.refresh(); await f.store.renew("preview-a");
  assert.equal(f.calls.length, count);
});

test("late heartbeat cannot revive ended instance or install fees", async () => {
  const f = fixture(); await f.store.refresh();
  const lease = deferred(); f.handlers.set("/previews/preview-a/heartbeat", () => lease.promise);
  const pending = f.store.renew("preview-a");
  f.values.set("a", computer("a", { terminal: true, status: "ended", credits: "5" }));
  await f.store.refresh();
  lease.resolve({ computer: computer("a", { credits: "999" }) }); await pending;
  assert.equal(f.store.getSnapshot().computers.a.credits, "5");
  assert(!previewReady(f.store.getSnapshot(), "preview-a"));
});

test("command cancellation is independent, standalone can view a cross-conversation instance", async () => {
  const f = fixture({ conversationId: null, previewId: "preview-a" });
  f.handlers.set("/cloud-computers/a/commands", () => ({ items: [
    { id: "cmd", computer_id: "a", status: "running", terminal: false, cancel_requested: false, result: {} },
  ] }));
  await f.store.refresh(); await f.store.cancel("cmd");
  assert(previewReady(f.store.getSnapshot(), "preview-a"));
  assert(previewReady(f.store.getSnapshot(), "preview-b"));
  assert(!f.calls.some(call => call.path.endsWith("/end")));
});

test("disposed scope ignores late writes and another workspace starts empty", async () => {
  const f = fixture(), pending = deferred();
  f.handlers.set("/cloud-computers?limit=20", () => pending.promise);
  const polling = f.store.refresh(); f.store.dispose();
  pending.resolve({ items: [], next_offset: null }); await polling;
  assert.deepEqual(f.store.getSnapshot().computers, {});
  assert.deepEqual(fixture({ workspaceId: "another" }).store.getSnapshot().computers, {});
});
