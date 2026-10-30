# Multi-file Agent Skills

> Historical acceptance only. Issue #61 replaces this runtime with [administrator-managed text skills](platform-skills-feedback.md). Personal bundles and files remain private and retained; their management and execution are disabled.

## Goal And Scope

Treat a Skill as a directory, using the Agent Skills specification
(https://agentskills.io/specification), rather than a single Markdown field.
Support directory/ZIP import, root SKILL.md editing, supporting text/binary file
management, whole-directory ZIP export, and on-demand Agent file reads.
Preserve optional frontmatter and existing single-file skills.

The existing owner plus workspace authorization remains unchanged. Skill
instructions and allowed-tools metadata do not grant additional permissions.
Scripts are stored and can be inspected/exported, but are not executed; package
installation, host filesystem access and a sandbox runtime are outside this task.
Binary assets are preserved/downloadable, not automatically interpreted by the LLM.

## Design

- Keep SKILL.md in the existing markdown column; add transactional, cascading
  supporting file rows with binary content. Bounded small Skill bundles belong
  together in PostgreSQL, avoiding cross-storage partial updates/orphan objects.
- Import a root SKILL.md or a single enclosing directory matching frontmatter name.
  ZIP is an import/export transport, not a requirement of the Skill standard.
- Preserve arbitrary supporting directories, including scripts, references,
  assets, agents, and license files.
- Product resource limits: 200 files, 5 MiB per file, 20 MiB uncompressed bundle,
  25 MiB ZIP upload. Reject traversal, links, ambiguous paths, duplicate names,
  file/directory collisions and encrypted/unsupported archives. Never extract to disk.
- Agent sees only selected Skill summaries initially. load_skill returns the full
  entrypoint, optional metadata, file manifest and runtime capabilities.
  read_skill_file reads UTF-8 text in bounded line ranges; binary reads return
  metadata rather than treating arbitrary bytes as text.

## Acceptance And Verification

- Import/edit/export/reimport a bundle containing nested references, script,
  optional frontmatter and a binary asset, preserving bytes.
- Files can be added, replaced, removed and downloaded in the management UI.
- Existing JSON markdown CRUD remains compatible and preserves supporting files
  when the files field is omitted.
- Foreign owner/workspace, unselected/deleted/disabled skills and unsafe paths
  cannot expose file contents. Failed updates are atomic.
- Scripted Agent test loads instructions, then reads a needed reference, without
  preloading reference bodies or persisting them in tool event summaries.
- Verify archive limits, migration upgrade/downgrade on an isolated database,
  backend tests, frontend build and desktop/mobile browser workflow.

## Results

2026-10-22, local Docker environment:

- 165 backend tests passed, including the existing retrieval, authorization,
  conversations, citations, credits and MCP regressions.
- Frontend TypeScript/Next.js production and API/Web Docker builds passed.
  No frontend dependency was added.
- An isolated PostgreSQL database completed 0015 -> 0016 -> 0015 -> 0016.
  The legacy markdown entrypoint survived every transition; binary file bytes
  were checked after each upgrade. The temporary database was removed.
- Real browser imported both a ZIP and a directory containing SKILL.md,
  references, scripts and a PNG. Edited the policy reference, created another
  text file, uploaded an additional file, removed a script, and saved.
  Authenticated API verification confirmed the resulting contents.
- A real configured cloud model called load_skill and read_skill_file, then
  returned policy code ASTER-824 and the browser-edited threshold of 41.
  Three successful Skill tool summaries persisted in answer details without
  exposing complete file contents in those summaries.
- Desktop 1440x900 and mobile 390x844 screenshots were inspected. On mobile,
  document width remained 390px and the modal stayed within the viewport.
- Downloaded PNG SHA-256 matched the source. Authenticated ZIP export was read
  with Python zipfile and verified to contain all five files, the updated
  reference, optional metadata and unchanged PNG bytes.
- Dedicated test Skills were deleted, the conversation soft-deleted, the
  temporary password randomized and all its sessions revoked. No user data,
  model credentials or existing document indexes were modified.

### Outstanding Browser Download Check

ZIP export is implemented and API roundtrip/byte checks pass, but the automated
browser's ZIP download acceptance is **not passed**. The backend returns HTTP 200
with the complete archive and the expected CORS headers to the authenticated API
client. Chrome's network diagnostics instead observe an empty HTTP 204 without
CORS headers for the download response, leading to a browser fetch failure.
The exact intervening component has not been identified.

Both direct navigation and an authenticated Blob download were attempted.
Changing MIME type did not resolve the discrepancy, so the standard
application/zip response was retained. No permission relaxation, arbitrary proxy,
base64 transport or additional infrastructure was introduced to bypass it.
The UI surfaces download errors. This remaining environment/browser check must
not be described as completed or used to close an associated acceptance issue.

Screenshots and test fixtures live under ignored output/playwright/.
