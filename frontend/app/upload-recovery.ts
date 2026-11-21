export function restoreUploadState(
  state: string,
  failure?: { stage?: string; retryable?: boolean },
) {
  if (state === "failed" && failure?.stage === "verification" && failure.retryable) return "failed";
  return ["uploading", "queued", "failed"].includes(state) ? "paused" : state;
}

export function isRestorableUploadTask(task: {
  key?: unknown;
  uploadId?: unknown;
  name?: unknown;
  size?: unknown;
  library?: { id?: unknown } | null;
}, maximumSize: number) {
  const uuid = /^[0-9a-f-]{36}$/i;
  return typeof task.key === "string" && uuid.test(task.key)
    && (task.uploadId === undefined || typeof task.uploadId === "string" && uuid.test(task.uploadId))
    && typeof task.name === "string"
    && typeof task.size === "number" && task.size > 0 && task.size <= maximumSize
    && typeof task.library?.id === "string";
}

export function requiresFreshUpload(code?: string) {
  return ["upload_expired", "integrity_mismatch", "invalid_format", "idempotency_conflict"].includes(code || "");
}

export function canResumeTransfer(failure?: { stage?: string; code?: string; retryable?: boolean }) {
  return !failure || failure.stage !== "verification"
    && (failure.retryable || failure.code === "transfer_failed");
}

export function uploadIssue(state: string, message?: string, retryLabel?: string, queryPaused = false) {
  if (!queryPaused && (state === "verifying" || retryLabel)) return;
  return message;
}

export function isUploadTaskPending(state: string) {
  return !["ready", "cancelled", "deleted"].includes(state);
}

export function canPollUploadTask(state: string, jobId?: string) {
  return ["verifying", "processing"].includes(state) || state === "processing_failed" && !jobId;
}
