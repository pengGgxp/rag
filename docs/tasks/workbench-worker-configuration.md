# Question workbench tools missing in background answers

## Goal and scope

Restore the existing question tools for explicitly selected, authorized
conversations executed by the ordinary Worker. Fix production configuration
propagation and release checks. Preserve grants, model behavior, prices and
existing questions; do not enable tools for unselected conversations.

## Evidence (2026-11-12)

The reported conversation selected the workbench and its account entitlement
and workspace grant were enabled. Its answer nevertheless said the workbench
was unavailable and had no tool calls. Production API received the protected
bridge secret; the ordinary Worker did not. The background runtime consequently
excluded all nine workbench tools. Previous health checks probed API only.

## Acceptance and verification

- Both API and ordinary Worker load the same protected bridge file.
- Configuration validation rejects either service omitting that file.
- Release checks reject missing or mismatched bridge configuration without
  logging credentials and probe connectivity from API and Worker.
- Local isolated PostgreSQL/Redis/application containers and a temporary user
  exercise normal browser selection and a real model tool call. No writes to
  the reported production conversation or production question database.
- Run deployment-contract and workbench regression tests and diff checks.
- Record actual browser results and any remaining unverified behavior.

## Results (2026-11-12)

- Production remains unchanged at v0.1.15. The reported conversation and
  production question database were not modified.
- Built local API/Worker and Web images from the current source. Applied all
  Alembic migrations to a new PostgreSQL database. Used separate Redis,
  question database and export storage, plus a temporary account and workspace.
  Only the selected model connection/pricing was copied for real model calls;
  production sandbox and object-storage settings were not imported.
- Through a real browser, logged in, selected and authorized the workbench,
  then used `gpt-6.1-sol` with high thinking. The model called `load_skill`
  and `save_questions`; the question database and normal SSO workbench page
  both showed exactly ten new questions.
- A follow-up used `search_questions`, `create_paper`, `export_paper` and
  `get_export`. One ten-question paper and one completed DOCX export existed.
  The actual answer link downloaded a 38,573-byte DOCX containing the title,
  first/last questions and an answer section. Reload restored the activities
  and working result links without repeating the writes.
- Directly verified the nine-tool runtime in the real local Worker: removing
  its bridge secret excludes the tools; restoring it restores availability.
  The new health-check function also passed against the real local API and
  Worker, including effective configuration comparison and both HTTP probes.
- Initial real model request failed after approximately 60 seconds with
  `response_uncertain`, before a question write. Only the isolated model
  connection timeout was raised from 60 to 180 seconds; a manual retry and
  subsequent export completed. The production timeout was not changed. This
  separate upstream interruption does not explain the original answer's
  missing tools.
- Local request accounting: failed request 0.0000 credits, successful save
  1.5401 credits, follow-up paper/export 3.0806 credits. These are isolated
  application ledger values, not a verified supplier invoice; no production
  user balance was charged by the test. Successful calls reported 103,849
  input tokens and 2,311 output tokens. The timed-out call reported no usage,
  so its supplier-side charge remains unknown.
- Deployment regression: 66 passed. Workbench regression: 6 passed. Frontend
  production build/type check and `git diff --check` passed.
- Reviewed browser screenshots in `output/playwright/`:
  `workbench-real-save-20261112.png`,
  `workbench-real-ten-questions-20261112.png`, and
  `workbench-real-export-restored-20261112.png`.

## Remaining delivery

The configuration and release-check fix requires a normal release before it
affects production. No deployment or production conversation retry was
performed as part of this investigation.
