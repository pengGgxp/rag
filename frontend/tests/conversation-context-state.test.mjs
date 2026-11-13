import assert from "node:assert/strict";
import test from "node:test";
import { acceptContextState } from "../app/conversation-context-state.ts";

test("late events cannot restore a replaced context window", () => {
  const current = { windowId: "new", contextRevision: 8, status: "completed" };
  assert.equal(acceptContextState(current, { windowId: "old", contextRevision: 7 }), current);
  assert.equal(acceptContextState(current, { windowId: "old", contextRevision: 8 }), current);
  const appended = { windowId: "new", contextRevision: 9, status: "idle" };
  assert.equal(acceptContextState(current, appended), appended);
});
