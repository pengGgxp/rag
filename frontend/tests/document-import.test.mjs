import assert from "node:assert/strict";
import test from "node:test";
import { BlobWriter, TextReader, ZipWriter } from "@zip.js/zip.js";
import { prepareDocuments, droppedDocuments } from "../app/document-import.ts";

const prepare = files => prepareDocuments(files, new AbortController().signal);
async function archive(entries, options = {}) {
  const writer = new ZipWriter(new BlobWriter(), { useWebWorkers: false });
  for (const [path, content, settings] of entries) await writer.add(path, new TextReader(content), { ...options, ...settings });
  return new File([await writer.close()], "资料.zip");
}

test("multi-select keeps more than twenty files and reports every unsupported item", async () => {
  const files = Array.from({ length: 25 }, (_, i) => new File([`document ${i}`], `${i}.txt`));
  const result = await prepare([...files, new File(["binary"], "run.exe"), new File([], "empty.md")]);
  assert.equal(result.files.length, 25);
  assert.equal(result.issues.length, 2);
});
test("folder paths and equal basenames remain distinct", async () => {
  const file = new File(["folder content"], "lesson.md");
  Object.defineProperty(file, "webkitRelativePath", { value: "课程/第一章/lesson.md" });
  const result = await prepare([file, await archive([["一/lesson.md", "one"], ["二/lesson.md", "two"], ["nested.zip", "zip"]])]);
  assert.equal(result.files.length, 3);
  assert.equal(result.files[0].path, "课程/第一章/lesson.md");
  assert.equal(await result.files[1].file.text(), "one");
  assert.equal(result.issues.length, 1);
});
test("dangerous paths, links and encryption reject the entire archive", async () => {
  for (const zip of [await archive([["../bad.txt", "bad"]]),
    await archive([["link.txt", "target", { externalFileAttributes: 0o120777 << 16 }]]),
    await archive([["secret.txt", "secret"]], { password: "private" })]) {
    const result = await prepare([zip]);
    assert.equal(result.files.length, 0);
    assert.equal(result.issues.length, 1);
  }
});
test("corrupt and excessive-compression archives are not admitted", async () => {
  const normal = await archive([["plain.txt", "abcdefg", { level: 0 }]]);
  const bytes = new Uint8Array(await normal.arrayBuffer());
  const offset = Buffer.from(bytes).indexOf(Buffer.from("abcdefg"));
  assert.ok(offset >= 0); bytes[offset] ^= 1;
  for (const zip of [new File([bytes], "broken.zip"), new File(["not zip"], "invalid.zip"),
    await archive([["huge.txt", "a".repeat(3 * 1024 * 1024)]])]) {
    const result = await prepare([zip]);
    assert.equal(result.files.length, 0);
    assert.equal(result.issues.length, 1);
  }
});
test("limits report files instead of silently dropping them; new version remains one document", async () => {
  const files = Array.from({ length: 201 }, (_, i) => new File(["text"], `${i}.txt`));
  const result = await prepare(files);
  assert.equal(result.files.length, 200); assert.equal(result.issues.length, 1);
  const single = await prepareDocuments(files.slice(0, 2), new AbortController().signal, true);
  assert.equal(single.files.length, 1); assert.equal(single.issues.length, 1);
  const cancelled = new AbortController(); cancelled.abort();
  await assert.rejects(prepareDocuments(files, cancelled.signal), { name: "AbortError" });
});
test("folder drag enumerates all reader pages and retains paths", async () => {
  let page = 0;
  const directory = { name: "目录", isDirectory: true, createReader: () => ({ readEntries: callback => {
    callback(page++ < 2 ? [{ name: `page${page}.txt`, isFile: true, file: resolve => resolve(new File(["content"], `page${page}.txt`)) }] : []);
  } }) };
  const files = await droppedDocuments({ items: [{ webkitGetAsEntry: () => directory }] }, new AbortController().signal);
  assert.equal(files.length, 2); assert.equal(files[1].webkitRelativePath, "目录/page2.txt");
});
