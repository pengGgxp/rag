import assert from "node:assert/strict";
import test from "node:test";
import { skillCatalog, filterSkills } from "../app/extensions/skill-catalog.ts";

const pkg = { id: "package", name: "报告", description: "仓库介绍", installed: true };
const own = { id: "file", name: "报告", description: "修改后的介绍", directory: ".skills/报告" };
test("installed state is discovered from files, not stale package status", () => {
  const [item] = skillCatalog([pkg], []);
  assert.equal(item.installations.length, 0);
  assert.equal(filterSkills([item], "", "available").length, 1);
});
test("one named card retains every workspace location and actual description", () => {
  const values = [own, { ...own, id: "second", directory: ".skills/报告 (2)" }];
  const [item] = skillCatalog([pkg], values);
  assert.equal(item.installations.length, 2);
  assert.equal(item.description, own.description);
  assert.equal(item.packageId, pkg.id);
  assert.equal(filterSkills([item], "报告 (2)", "installed").length, 1);
  assert.deepEqual(values, [own, { ...own, id: "second", directory: ".skills/报告 (2)" }]);
});
test("imports remain visible and broken nameless manifests never merge", () => {
  const items = skillCatalog([], [own, { id: "bad1", name: null, description: "", directory: ".skills/坏清单", manifest_error: "缺少名称" }, { id: "bad2", name: null, description: "", directory: ".skills/坏清单2", manifest_error: "解析错误" }]);
  assert.equal(items.length, 3);
  assert.equal(items[1].needsRepair, true);
  assert.equal(items[1].name, "坏清单");
  assert.equal(filterSkills(items, "不存在", "").length, 0);
});
test("exact names match even when description needs repair; changed names detach", () => {
  const items = skillCatalog([pkg], [{ ...own, description: "", manifest_error: "简介格式错误" }, { ...own, id: "other", name: "报告新版" }]);
  assert.equal(items.length, 2);
  assert.equal(items[0].needsRepair, true);
  assert.equal(items[0].installations.length, 1);
  assert.equal(items[1].packageId, undefined);
});
