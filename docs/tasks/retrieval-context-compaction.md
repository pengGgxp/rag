# Retrieval identifiers and repeated context compaction

## Goal and scope

Investigate conversation dda9aac2-1b7f-47cf-aa6b-46f44bf064f3 using
read-only production evidence. Fix document-scoped retrieval rejecting IDs
returned by retrieval itself and ineffective automatic compaction. Keep
workspace permissions, active-version checks, original messages and billing
rules intact. Do not change production data or deploy in this task.

## Evidence (2026-11-12)

- Four document-scoped searches returned a generic validation error. Retrieval
  evidence exposes a stable document ID; the validator looks up only the
  version ID. The stable ID in the reported evidence has no Document row,
  while its active version exists and is ready. Cancelled executions do not
  retain their full private tool arguments, so the exact four payloads cannot
  be recovered from the persisted activities.
- One retry failed after reporting compaction completed: estimated input
  dropped from 165425 to 150037, still above its 141000 input budget.
- A later retry reduced 134345 to 134230, then immediately compacted again.
  Its final compaction was cancelled, rather than a provider failure.

## Acceptance and verification

- Stable document IDs resolve to their current ready version within the
  selected authorized libraries. Explicit inactive version IDs, deleted
  documents and inaccessible libraries remain rejected. Errors identify
  the actionable validation condition without disclosing private content.
- Automatic compaction selects meaningful complete tool groups, preserves
  the current user input, avoids immediately repeating ineffective work, and
  never persists or reports success for an over-budget result.
- Run focused retrieval, streaming, attachment and context regressions.
  Exercise an isolated PostgreSQL/API/Worker/Web setup through a browser
  using synthetic documents, images and a temporary user with the same
  production model configuration. Never retry in the reported conversation.

## Implementation and results (2026-11-12)

- Retrieval accepts stable document IDs and active version IDs, normalizes
  both to the current version for retrieval, and deduplicates the resulting
  scope. Inactive explicit version IDs are not silently upgraded. Selected
  library permissions are checked before and after resolution. The tool
  schema excludes empty ID arrays and explains the identifier contract.
- Validation activities show the actual safe validation condition instead
  of a generic failure. No private file body or credentials are exposed.
- Automatic compaction skips negligible groups, merges earlier in-run
  summaries and includes larger complete call/result groups when needed.
  After an attempt it waits for meaningful input growth before retrying;
  the hard input budget always overrides that wait. At the hard limit, even
  small sufficient reductions remain allowed. Current user images remain
  untouched. Over-budget summaries are rejected before persistence or a
  completed activity.
- Ran fresh local PostgreSQL migrations through 0065, API, ordinary Worker
  and the existing same-source Web image. Created a temporary user and two
  synthetic knowledge documents; no production storage, sandbox, workbench
  or payment credentials were imported.
- A real browser sent a long-history task using the production-configured
  gpt-6.1-sol Responses model, high thinking, and unchanged 60-second
  connection timeout. One summary reduced estimated input from 115905 to
  62954. Discovery, one broad search and two stable-ID-scoped searches all
  succeeded; the final answer had two verifiable citations. Refresh restored
  the completed activities and answer. Actual provider-reported usage across
  five calls: 122796 input and 1042 output tokens. The isolated test ledger
  settled 149.0673 credits. This is not a production user balance adjustment.
- Browser artifacts were inspected: context-live-completed-20261112.png and
  context-live-activities-20261112.png under output/playwright.
- Final focused regression suite: 245 passed (Agent, context budgeting,
  conversations, streaming, attachments, image reading and upstream retries).
  git diff --check passed. No frontend files or database schema changed.
- The initial local fixture incorrectly left next_sequence at zero after
  seeding eight messages. It was corrected before model execution; the local
  retry then completed. This fixture error did not affect production.
- Image preservation was verified with synthetic attachment descriptors at
  the reported image dimensions and attachment regressions. The real-model
  browser run used text documents; the original private images were not
  copied or replayed.
- Removed temporary containers, volumes and plaintext exported model
  credentials from both machines. Production remains v0.1.15; no deployment
  or original-conversation retry was performed.
