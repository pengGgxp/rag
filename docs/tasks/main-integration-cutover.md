# Main integration cutover

Goal: remove the split between `codex/resource-library` and `main`, verify the
merged application against production behavior, and deploy the canonical main
release without data loss.

Out of scope: schema changes beyond the existing `0062_conversation_management`
head, price or permission changes, and real paid model or supplier calls.

Completed:

- Merged the nested Skill-root fix into the resource-library history and corrected
  stale Skill/cloud test expectations with a separate test commit.
- Pushed the verified integration branch and updated `origin/main` to `d1f1d07`.
- Preserved a verified production snapshot at
  `/data1/rag-releases/pre-main-integration-20261109`, including database dump,
  source, Compose inputs, container metadata, image IDs and SHA-256 checks.
- Built tagged release images on the production host from the tracked source
  archive. No untracked `node_modules` or local secrets were included.
- Switched only API, Worker, preview Worker and Web. Data services and the
  preview gateway retained their existing configuration.

Verification:

- Backend regression: 899 passed, 67 skipped, 0 failed.
- Isolated PostgreSQL: migration reached `0062_conversation_management` and was
  idempotent; focused PostgreSQL suite: 7 passed, 4 skipped.
- Frontend production build passed. Full isolated Compose stack started with
  healthy API/database/Redis/object storage; `/resources` returned 200.
- Before switching, no active answers, ingestion jobs, image tasks or cloud
  sessions remained.
- After switching, API/Worker/preview Worker/Web are running on the tagged
  release images. Public API health, `/resources` and `/payments/checkout`
  returned 200. All application Python file contents match `d1f1d07`; no
  application exceptions appeared in deployment-window logs.

Release records:

- Candidate release: `/data1/rag-releases/main-d1f1d07-20261109`.
- Rollback snapshot: `/data1/rag-releases/pre-main-integration-20261109`.
- No real model calls were made for this cutover.
