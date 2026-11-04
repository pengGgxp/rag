# Resource Library Implementation Task

## Goal
Unify persistent chat uploads and generated workspace files into a workspace-shared resource library, and prepare only explicitly requested resource versions for isolated model execution.

## Scope
- Preserve existing objects and chat history while migrating attachment records into resources.
- Add resource upload, browsing, selection and deletion flows.
- Stop age-based deletion of valid resources; enforce shared storage capacity and 30-day over-quota cleanup without deleting knowledge-base documents.
- Charge model-originated object reads against workspace download allowance, with bounded external cache support.
- Replace whole-workspace executor staging with explicit resource inputs.

## Out of Scope
- Online deployment, pricing changes, knowledge-base ingestion of resources, private-resource classification, and deleting knowledge-base documents.

## Acceptance
- Existing chat uploads and generated files remain visible and downloadable after migration; deleting a conversation does not delete resources.
- Workspace members can reuse shared resources in chat; cross-workspace references fail.
- Commands receive only requested resources; no-resource commands stage no resource files.
- Actual model reads consume download allowance, cache hits do not, and user downloads remain metered.
- Storage excess blocks new writes and triggers oldest-first resource cleanup only after 30 continuous days.
- Backend tests, frontend build and `git diff --check` pass; deployment is not performed.

## Local Verification (2026-11-04)

- Relevant backend regression: 135 passed, 2 skipped (real Docker runner checks).
- Final resource/storage regression after quota-clock adjustment: 31 passed, 5 skipped. PostgreSQL checks require a disposable `AUTH_TEST_DATABASE_URL`; none was available. A migration probe now covers preserved object keys, message links, path collisions, grace reset and conversation deletion when that environment is provided.
- `npm run build` passed, Alembic reports a single `0053_resource_library` head, Compose YAML parsed with shared API/Worker cache and no executor cache mount, and `git diff --check` passed.
- Playwright CLI used a disposable SQLite account database, actual API endpoints and synthetic object storage/model. Checked 1440/700/390px resource lists, no horizontal overflow, search/empty state, upload, text preview, Escape, deletion confirmation, selecting resources into chat, and removing a draft without deleting its resource. Actual admin/editor/viewer sessions verified UI write controls. The imminent-overage banner was checked using an intercepted quota response; backend grace and cleanup were separately tested.
- Screenshots saved under `output/playwright/resources/` and reviewed, including desktop list, mobile list, chat selection and role states. Fixed a global button centering style discovered during image inspection.
- Temporary fixture API/Web processes were stopped after verification; no real user data, paid model calls or online deployment were used. Existing unrelated edits and `output/` remain intact.

## Acceptance Still Open

- Docker Desktop's engine did not respond, so no real container/network or PostgreSQL migration result is claimed.
- Real COS + container + model workflow (image into document, new version, subsequent chat reuse) and exact live byte reconciliation remain unverified.
- Full keyboard/focus, interrupted browser uploads and all modal states at every viewport are not claimed as complete. The final small read-only chat-upload guard was build-checked after the browser run.
- Do not deploy or close acceptance until these integration checks pass. Online rollout requires backups and stopping old cleanup workers before migration.
