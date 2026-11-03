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
