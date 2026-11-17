import test from "node:test";
import assert from "node:assert/strict";
import { cachedPreview, clearPreviewCache } from "../app/resource-preview-cache.ts";

test("cache isolates account, workspace, identity, revision and byte ranges", async () => {
  await clearPreviewCache();
  let reads = 0;
  const load = async () => new Blob([`body${++reads}`]);
  const read = (scope = "alice:work", revision = "v1", id = "file", range) => cachedPreview(scope, revision, id, load, range);
  assert.equal(await (await read()).text(), "body1");
  assert.equal(await (await read()).text(), "body1");
  await read("bob:work"); await read("alice:other"); await read("alice:work", "v2"); await read("alice:work", "v1", "other");
  assert.equal(reads, 5);
  await clearPreviewCache("alice");
  assert.equal(await (await read("bob:work")).text(), "body2");
  await read(); assert.equal(reads, 6);
  assert.equal((await cachedPreview("alice:work", "v1", "file", async () => new Blob(["ab"]), [0, 2])).size, 2);
  await assert.rejects(cachedPreview("alice:work", "v1", "file", async () => new Blob(["bad"]), [2, 4]), /完整/);
});

test("clearing while a read is pending does not persist late content", async () => {
  await clearPreviewCache();
  let release;
  const pending = cachedPreview("alice:work", "v1", "pending", () => new Promise(resolve => { release = resolve; }));
  await new Promise(resolve => setTimeout(resolve, 0));
  await clearPreviewCache("alice");
  release(new Blob(["old"]));
  await pending;
  assert.equal(await (await cachedPreview("alice:work", "v1", "pending", async () => new Blob(["new"]))).text(), "new");
});
