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
