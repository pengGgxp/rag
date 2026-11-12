import assert from "node:assert/strict";
import test from "node:test";
import { activityItem } from "../app/conversation-display.ts";

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
