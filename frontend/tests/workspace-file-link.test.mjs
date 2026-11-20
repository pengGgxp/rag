import assert from "node:assert/strict";
import test from "node:test";
import { workspaceFileId } from "../app/workspace-file-link.ts";
import { activityFiles, activityItem } from "../app/conversation-display.ts";

const id = "34ac1c44-bcd3-4817-86dc-660404fdef18";
test("仅拦截本站真实文件页面链接，保留普通链接及引用", () => {
  assert.equal(workspaceFileId(`/resources?file=${id}`), id);
  assert.equal(workspaceFileId(`https://zk.example/resources?file=${id}`, "https://zk.example"), id);
  for (const href of ["#source-1", "/resources", "/resources?file=bad", `//other.test/resources?file=${id}`,
    `https://other.test/resources?file=${id}`, `/resources?file=${id}&file=${id}`, "javascript:alert(1)"]) {
    assert.equal(workspaceFileId(href, "https://zk.example"), null);
  }
});

test("跨处理段、读取和生图更新汇总为同级文件，保持首次顺序与删除状态", () => {
  const file = { id, path: "图.png", content_type: "image/png", url: `/resources?file=${id}` };
  const other = { id: "other", path: "script.py" };
  const event = extra => activityItem({ call_id: "call", name: "read_file", label: "读取", kind: "tool", status: "completed", ...extra });
  const inputs = [
    event({ files: [file, other] }),
    event({ image_task: { files: [{ id, path: "图.png", version: 1 }] } }),
    event({ files: [{ ...file, availability: "missing" }] }),
    event({ name: "list_files" }),
  ];
  const original = structuredClone(inputs);
  assert.deepEqual(activityFiles(inputs), [{ ...file, version: 1, availability: "missing" }]);
  assert.deepEqual(inputs, original);
});
