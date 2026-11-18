import assert from "node:assert/strict";
import test from "node:test";
import { pdfPreviewError } from "../app/pdf-preview-error.ts";

test("password failures give an opening-password action and preserve download guidance", () => {
  for (const code of [1, 2]) {
    const message = pdfPreviewError({ name: "PasswordException", code });
    assert.match(message, /需要打开密码/);
    assert.match(message, /下载原文件/);
    assert.match(message, /移除打开密码后重新上传/);
    assert.doesNotMatch(message, /损坏/);
  }
});

test("empty and corrupt PDFs have distinct actions", () => {
  for (const message of ["The PDF file is empty, i.e. its size is zero bytes.", "The document contains no pages."]) {
    assert.match(pdfPreviewError({ name: "InvalidPDFException", message }), /为空或没有页面/);
  }
  assert.match(pdfPreviewError({ name: "InvalidPDFException", message: "Invalid PDF structure." }), /已损坏或格式无效/);
});

test("unexpected errors do not expose worker diagnostics or document content", () => {
  for (const cause of [undefined, null, "private content", new Error("private URL and document content"), {}]) {
    assert.equal(pdfPreviewError(cause), "PDF 无法打开，请重新打开或下载原文件查看");
  }
});
