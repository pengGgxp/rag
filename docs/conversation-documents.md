# Conversation outputs as knowledge documents (Issue #74)

Goal: explicitly requested text outputs become new Markdown documents through the
same upload admission, object storage, parsing, embedding, indexing and billing
services as ordinary uploads. No response action, separate page, attachment copy,
new tool fee or document overwrite is added.

The model may offer to preserve reusable reports or procedures once. It must wait
for explicit user instruction/confirmation, establish title/content/destination,
and explain the destination's existing visibility and normal upload usage costs.
This conversational consent is enforced by the system instruction, not presented
as a deterministic authorization classifier. Tool calls independently enforce real
account, workspace and library write permissions; retrieved text is not consent.

`list_writable_knowledge_bases` returns only IDs/names across the current workspace.
It does not change the conversation's retrieval scope. `save_to_knowledge_base`
accepts `knowledge_base_id`, `title` (up to 200 characters) and `markdown` (up to
200,000 characters). `get_saved_document_status` accepts a returned `save_id`.
The authenticated read-only `GET /conversation-document-saves/{save_id}` supports
UI progress without model polling; records belong to their original user/workspace.

Migration 0047 adds independent AI-origin document labeling and private operation
receipts. Receipts bind server-owned request/message/conversation identity, content
hash, document, job and validated citation IDs. A library row lock serializes normal
content deduplication and receipt admission, backed by a database unique constraint.
Identical later requests reuse a non-deleted document without new processing/fees.
Same-request retries also reuse the receipt. Deleting a document does not silently
create a replacement when its original save request is replayed.

The upload service cleans object writes when commit fails. Committed submissions
retain a pending-dispatch flag until dispatch succeeds; API startup and a 30-second
recovery loop retry pending dispatch through the original task. Status reads never
dispatch jobs. Existing job state/locks and usage idempotency remain authoritative.
Stopping chat after commit leaves the document intact. Failed/cancelled documents
use existing explicit retry and deletion controls.

Only a public `content_origin=ai_assisted` label is exposed in preview/retrieval;
private chat IDs and original citation relationships are not exposed to other
knowledge-library members. Stored provenance accepts only citations actually
available from this conversation and rechecks current source access. Source content
remains untrusted. Being saved does not certify model-generated claims as facts.

UI reuses ordered conversation activities, the existing document list/preview and
download-independent links. It refreshes visible saved results while the page is
visible, stops at terminal states, and verifies once on refresh even for old terminal
snapshots. Only READY means searchable. No keys, storage paths or parser diagnostics
are added to normal activity copy.

Acceptance: permissions/revocation, malformed input, atomic receipts and content
deduplication, queue failures/recovery, origin privacy, local Markdown without OCR,
existing model/storage/embedding billing, retrieval/citation traceability, real
PostgreSQL migration/concurrency, responsive browser screenshots and a real
synthetic model -> save -> ingest -> retrieval workflow. Do not close #74 before
the actual verification and deployment evidence is recorded.

## Verification record (2026-11-03)

- Focused backend regressions: 70 passed (save tools, uploads, capacity, extensions,
  request models and attachments). Related streaming/context/storage/job and Agent
  regressions also passed. The initial complete local run found an outdated tool
  whitelist assertion (updated) and an intermittent SQLite UUID numeric-affinity
  conversion failure; both targeted reruns passed.
- A disposable pgvector PostgreSQL database migrated from 0001 to 0047. Save
  concurrency, COS upload and billing checks passed; concurrent identical saves
  create one document, one receipt and one queue dispatch.
- Frontend production build and `git diff --check` passed. Playwright checked
  ordered activity, document preview/link, refresh, failure and network recovery
  at 1440/700/390px with reduced motion; screenshots were visually inspected.
  Layout fixtures are synthetic UI data, not evidence of actual ingestion.
- Private screenshots and synthetic test artifacts remain in ignored `output/`
  directories. No real user documents, chat content or credentials are committed.
- Server-image complete regression: **703 passed, 51 skipped**, 925.39 seconds.
  The first collection attempt lacked the experiment module, which is intentionally
  outside the runtime image; rerunning with a read-only experiment-directory mount
  passed. PostgreSQL-specific checks ran separately against the disposable database.
- Live acceptance used a dedicated account, `deepseek-flash`, and a 465-byte
  synthetic maintenance procedure. Drafting did not save a document. Explicit
  confirmation invoked the real save tool; the existing Worker completed local
  `plain-text` parsing and embedding, reaching READY. Replaying the same request
  added no document, receipt or usage. A subsequent model turn retrieved the new
  version/chunk and cited it with `content_origin=ai_assisted` (the model corrected
  an initially invalid retrieval argument before searching successfully).
- Actual platform spend for drafting, saving and retrieval/answer: **3.4795 credits**,
  including **0.0043 credits / 43 embedding tokens**, no OCR usage or separate tool
  fee. Provider-side RMB expense is not inferred from credits. Storage continues
  on the existing scheduled storage policy; this short run does not claim a full
  storage billing interval was observed.
- Public browser checks opened the real saved-document activity link and existing
  preview with the AI label. A final prompt adjustment requests natural-language
  states instead of raw status codes; 25 Agent tests passed afterward.
- Deployed migration 0047, API and Web on 2026-11-03. Backups, rollback images,
  migration/build/test logs and before/after invariants are under
  `/data1/rag-releases/conversation-save74-20261103`. Existing model settings,
  users, balances, ledger and document records matched before/after migration;
  `.env` and connector configuration matched. Worker, executor and data-service
  container IDs remained unchanged. Public health passed after the final update.
- Residual limit: natural-language consent and faithful source summarization remain
  model instruction responsibilities, not a deterministic fact/consent verifier.
