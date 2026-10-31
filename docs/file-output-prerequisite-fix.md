# File output prerequisites and failure feedback

## Task boundary

Fix file output failures caused by requiring a loaded script Skill for ordinary
file tools, and replace misleading connection/authorization advice. Keep workspace
membership and role checks, selected private package isolation, and isolated script
execution. Do not add user uploads, a file manager, new authorization dialogs, or
change billing. Issue #69; refs #67, #66.

## Acceptance and verification

- An authorized request can list and generate temporary output before loading a
  Skill, including when no script Skill is selected.
- Script execution still requires a selected, loaded, enabled script Skill and a
  configured isolated executor. Role changes take effect on the next tool call.
- Validation, conflicts, storage failures and script failures produce safe,
  actionable activity summaries without leaking remote error bodies.
- Run targeted backend tests and real isolated COS write/download and script
  execution checks before deploying the API. Preserve user data and `output/`.

## Verified results (2026-10-31)

- 54 backend regressions passed; two local Docker integration tests were skipped.
  Real isolated execution was instead verified against the online executor.
- A request with a selected script Skill listed and wrote a COS output before
  loading that Skill. The downloaded bytes matched exactly.
- Real deepseek-flash generated a second file using write_file with no Skill
  selected, and the authenticated download matched the expected content.
- A separate real model request loaded a synthetic private Skill, ran its Python
  script in the isolated executor, read the resulting output and downloaded it.
  Execution records were verified. Temporary test objects were removed; no
  production user's balance was changed.
- API deployed at 12:30:17 Beijing time after database/configuration backup, a
  rollback image, and confirmation that no chat generation was active. No schema,
  frontend, Worker, model price or default changes were required.
- The cancelled historical record does not retain its exact tool error; its
  list/write-before-load sequence matches the reproduced prerequisite defect.
  This fix does not re-execute that cancelled request or its side effects.

## Download links follow-up (Issue #70)

- A successful save includes only file ID, relative path, size and version in its
  tool activity. The authenticated download action appears under that exact call
  and remains available from saved conversation history.
- The model is instructed to name confirmed output files and direct users to the
  activity download action without inventing a URL or claiming unsaved files.
