import assert from "node:assert/strict";
import test from "node:test";
import { activityFiles, activityItem, appendActivity, processBlocks, processHeadline } from "../app/conversation-display.ts";

test("interrupted tools do not claim the user cancelled them", () => {
  const event = { call_id: "write", name: "write_file", label: "生成文件", kind: "mcp" };
  assert.equal(activityItem({ ...event, status: "interrupted" }).status, "uncertain");
  assert.equal(activityItem({ ...event, status: "cancelled" }).status, "cancelled");
  assert.equal(activityItem({ ...event, status: "completed" }).status, "completed");
  assert.equal(activityItem({ ...event, status: "error" }).status, "failed");
});

test("knowledge base activity preserves real object for navigation and replay", () => {
  const knowledge_base = { id: "actual-id", name: "真实知识库" };
  const result = activityItem({ call_id: "kb", name: "create_knowledge_base", label: "创建知识库",
    kind: "knowledge", status: "completed", summary: "已创建「真实知识库」", knowledge_base });
  assert.deepEqual(result.knowledge_base, knowledge_base);
  assert.equal(result.summary, "已创建「真实知识库」");
  assert.equal(result.status, "completed");
});

const event = (id, status = "completed", extra = {}) => ({
  call_id: id, name: "write_file", label: "生成文件", kind: "tool", status, ...extra,
});

test("validation feedback is neutral and never hides blocked or unknown outcomes", () => {
  assert.equal(activityItem(event("bad", "error", { outcome: "feedback" })).status, "feedback");
  assert.equal(activityItem(event("blocked", "error", { outcome: "blocked" })).status, "blocked");
  assert.equal(activityItem(event("unknown", "error", { outcome: "uncertain" })).status, "uncertain");
  assert.equal(activityItem(event("old", "error")).status, "failed");
  assert.equal(activityItem(event("running", "running", { outcome: "feedback" })).status, "running");
});

test("grouping absorbs thinking but preserves assistant text boundaries and canonical records", () => {
  const blocks = [
    { id: "commentary", type: "text", text: "我来处理。" },
    { id: "think1", type: "thinking", round: 1, status: "completed" },
    { id: "a", type: "activity", call_id: "bad" },
    { id: "think2", type: "thinking", round: 2, status: "completed" },
    { id: "b", type: "activity", call_id: "fixed" },
    { id: "answer", type: "text", text: "结果" },
    { id: "c", type: "activity", call_id: "other" },
  ];
  const original = structuredClone(blocks);
  const grouped = processBlocks(blocks);
  assert.deepEqual(grouped.map(item => item.type), ["text", "process", "text", "process"]);
  assert.deepEqual(grouped[1].blocks.map(item => item.id), ["think1", "a", "think2", "b"]);
  assert.deepEqual(blocks, original);
  assert.deepEqual(processBlocks(structuredClone(blocks)), grouped);
});

test("process identity and call upserts remain stable as streaming details arrive", () => {
  let blocks = appendActivity([], "bad");
  const id = processBlocks(blocks)[0].id;
  blocks = appendActivity(blocks, "bad");
  blocks = appendActivity(blocks, "fixed");
  assert.equal(blocks.length, 2);
  assert.equal(processBlocks(blocks)[0].id, id);
  assert.equal(processBlocks(blocks)[0].blocks.length, 2);
});

test("headline reports actual running step without turning observations into task failures", () => {
  const feedback = activityItem(event("bad", "error", { outcome: "feedback" }));
  assert.equal(processHeadline([feedback], false), "正在调整操作");
  assert.equal(processHeadline([feedback, activityItem(event("fixed", "running"))], false), "生成文件 · 进行中");
  assert.equal(processHeadline([feedback, activityItem(event("fixed"))], true), "处理记录 · 2 项操作");
  assert.equal(processHeadline([activityItem(event("failed", "error"))], true), "处理记录 · 1 项操作");
});

test("all file artifacts survive grouping and repeated updates use current file metadata", () => {
  const a = { id: "a", path: "a.md", version: 1, size_bytes: 20 };
  const b = { id: "b", path: "b.pdf", version: 1, size_bytes: 30 };
  const updated = { ...a, version: 2, size_bytes: 40 };
  assert.deepEqual(activityFiles([
    activityItem(event("first", "completed", { files: [a, b] })),
    activityItem(event("second", "completed", { files: [updated] })),
  ]), [updated, b]);
});
