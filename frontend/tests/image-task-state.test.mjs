import assert from "node:assert/strict";
import test from "node:test";
import { latestImageTask } from "../app/image-task-state.ts";
const task = (status, seconds) => ({ status, terminal: !["running", "saving"].includes(status),
  updated_at: `2026-11-16T03:00:0${seconds}Z` });

test("最终 SSE 结果覆盖较早的状态查询", () => {
  const running = task("running", 1), done = task("completed", 2);
  assert.equal(latestImageTask(running, done), done);
  assert.equal(latestImageTask(done, running), done);
});

test("重试保存接受较新的非终态，延迟响应不能倒退结果", () => {
  const failed = task("save_failed", 1), saving = task("saving", 2), done = task("completed", 3);
  assert.equal(latestImageTask(failed, saving), saving);
  assert.equal(latestImageTask(done, saving), done);
});

test("没有时间戳的历史任务优先展示已确认终态", () => {
  const running = { terminal: false }, done = { terminal: true };
  assert.equal(latestImageTask(done, running), done);
  assert.equal(latestImageTask(running, done), done);
});
