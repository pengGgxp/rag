import assert from "node:assert/strict";
import test from "node:test";

import { canPollUploadTask, canResumeTransfer, isRestorableUploadTask, isUploadTaskPending, restoreUploadState, requiresFreshUpload, uploadIssue } from "../app/upload-recovery.ts";
import { documentIssue, documentRetryLabel } from "../app/workspace-types.ts";

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

test("automatic verification recovery does not display a failure, but terminal and paused queries do", () => {
  assert.equal(uploadIssue("verifying", "temporary outage", "retrying"), undefined);
  assert.equal(uploadIssue("verifying", "query paused", "retrying", true), "query paused");
  assert.equal(uploadIssue("failed", "retry verification"), "retry verification");
});

test("unfinished count keeps failed processing tasks visible but excludes dismissed tasks", () => {
  assert.equal(isUploadTaskPending("failed"), true);
  assert.equal(isUploadTaskPending("processing_failed"), true);
  assert.equal(isUploadTaskPending("ready"), false);
  assert.equal(isUploadTaskPending("cancelled"), false);
  assert.equal(isUploadTaskPending("deleted"), false);
});

test("restored processing failures without a job ID can recover their retry target", () => {
  assert.equal(canPollUploadTask("processing_failed"), true);
  assert.equal(canPollUploadTask("processing_failed", "job-1"), false);
  assert.equal(canPollUploadTask("processing"), true);
  assert.equal(canPollUploadTask("ready"), false);
});

test("document alerts distinguish scheduled retries from terminal failures", () => {
  const retryAfter = "2026-11-20T10:00:00Z";
  const failure = { message: "temporary outage", retryable: true };
  assert.equal(documentRetryLabel("processing", "queued", retryAfter), "解析中 · 正在自动重试");
  assert.equal(documentRetryLabel("processing", "running", null, { number: 1, maximum: 3 }), "正在自动重试 1/3");
  assert.equal(documentIssue({ status: "processing", failure, job: { state: "queued", retry_after: retryAfter } }), undefined);
  assert.equal(documentIssue({ status: "failed", failure, job: { state: "failed", retry_after: retryAfter } }), failure.message);
  assert.equal(documentRetryLabel("processing", "queued", null), undefined);
});
