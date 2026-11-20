import assert from "node:assert/strict";
import test from "node:test";

import { canResumeTransfer, isRestorableUploadTask, restoreUploadState, requiresFreshUpload } from "../app/upload-recovery.ts";

test("keeps recoverable verification failures actionable after reload", () => {
  assert.equal(restoreUploadState("failed", { stage: "verification", retryable: true }), "failed");
});

test("pauses interrupted transfers that need the original file", () => {
  assert.equal(restoreUploadState("failed", { stage: "upload", retryable: true }), "paused");
  assert.equal(restoreUploadState("uploading"), "paused");
  assert.equal(restoreUploadState("processing"), "processing");
});

test("preserves an idempotent create attempt even before its upload ID is known", () => {
  const task = {
    key: "77777777-7777-4777-8777-777777777777",
    name: "source.pdf",
    size: 20,
    library: { id: "library-id" },
  };
  assert.equal(isRestorableUploadTask(task, 100), true);
  assert.equal(isRestorableUploadTask({ ...task, key: "not-a-uuid" }, 100), false);
});

test("recognizes permanent file failures that require a new upload session", () => {
  assert.equal(requiresFreshUpload("upload_expired"), true);
  assert.equal(requiresFreshUpload("integrity_mismatch"), true);
  assert.equal(requiresFreshUpload("invalid_format"), true);
  assert.equal(requiresFreshUpload("duplicate_document"), false);
});

test("restored transport failures can reselect the original file, unlike verification or permission failures", () => {
  assert.equal(canResumeTransfer({ stage: "upload", retryable: true }), true);
  assert.equal(canResumeTransfer({ stage: "upload", code: "transfer_failed" }), true);
  assert.equal(canResumeTransfer({ stage: "verification", retryable: true }), false);
  assert.equal(canResumeTransfer({ stage: "upload", code: "permission_denied", retryable: false }), false);
});
