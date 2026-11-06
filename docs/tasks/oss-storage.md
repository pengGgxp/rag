# OSS storage integration and cutover

## Goal and scope

Add independently encrypted COS/OSS connections to the existing storage console,
normalize file operations, and migrate all database-referenced persistent objects
to OSS with resumable verification before atomically changing the active store.
Do not implement sandbox mounts or previews, change prices or billing records,
delete source objects, or change resource keys and permissions.

## Acceptance and validation

- Configuration permissions, encryption, optimistic conflicts, safe credential rotation.
- Both adapters cover ordinary/multipart uploads, ranges, listings and private URLs.
- Manifest includes document history, retained resource versions and attachments;
  missing objects or mismatched hashes prevent cutover. Resume and reverse migration
  use the same verified path. Existing source data remains intact.
- Run focused backend tests, PostgreSQL migrations, frontend build and responsive
  browser screenshot review. Live OSS tests require administrator-provided bucket,
  dedicated credentials and exact-origin CORS; never infer completion without them.
- Back up before production changes, drain uploads/jobs/cloud sessions and stop
  mutation/cleanup processes during migration. No real model or paid OCR tests.

## Progress

- Inspected current COS configuration, direct multipart upload, OCR reads and legacy
  document-only migration. Existing unrelated working-tree changes are preserved.
- Implemented independently encrypted provider connections, official OSS V2 adapter,
  coordinated upload/worker/storage reads, and a resumable verified migration CLI.
- Final local regression: 128 passed (three dependency deprecation warnings);
  git diff --check passed. No production ledger or user content was modified.
- PostgreSQL disposable-container validation: base-to-0058 migrations and 37 tests
  passed, including provider uniqueness, concurrent configuration updates and the
  migration advisory lock. Production database and services were not changed.
- Frontend production build passed. Mocked browser flows passed at 1440, 700 and
  390px; all nine saved screenshots were visually inspected. Covered dirty drafts,
  save/test, conflict recovery, masked credentials and active-location protection.
- Real OSS validation (2026-11-06): administrator-provided private bucket in
  cn-beijing passed ordinary upload/download with checksum, HEAD, Range, multipart
  completion/abort, listings, signed browser-style part PUT and object cleanup.
  Anonymous reads were denied. No real model or paid OCR was invoked.
- The live check exposed SDK StreamBodyReader.read() lacking a size argument.
  Both full and Range downloads now spool iter_bytes() to file-like temporary
  streams; the regression fixture follows the actual SDK stream contract.
- Production CORS preflight for https://zk.str1ct.top returned 403 without allowed
  origin or exposed ETag. Administrator action requested. Do not switch storage
  until browser upload readiness is confirmed. Online deployment/cutover remains
  pending; COS is unchanged and source objects have not been deleted.
- Administrator corrected CORS: production-origin preflight and actual signed PUT
  now both return allowed origin and exposed ETag. Preparing an isolated release
  from live source plus storage changes; keep the existing execution runtime.
- Actual Chromium upload from the production origin returned 200 with readable
  ETag; completed object contents matched. Temporary multipart/object checks were
  removed. SDK OperationError wraps service 404s; normalize this wrapper before
  treating absent target objects as missing. Final 23 focused tests passed locally
  and in the release image.
- Release-image PostgreSQL validation: 41 passed, including a synthetic COS
  connection inserted before 0058 and retained after upgrade. Disposable databases
  and containers were removed. Release Web build passed with bounded memory.
- Live source preflight: 41 unique referenced objects / 102294804 bytes, zero
  missing objects and zero size mismatches. Backing up before offline cutover.
- Production cutover completed on 2026-11-06. Migration
  `135aaebb-8ce5-44fa-aece-bf9d43fbbbf0` copied and twice verified all 41 objects,
  then atomically activated OSS. COS objects and encrypted connection are retained.
- Offline synthetic authenticated API validation passed: resource upload, full and
  Range download, deletion and hidden-after-deletion behavior. Synthetic identity,
  workspace and metering rows were rolled back; temporary OSS object was removed.
  User/workspace/membership, document/resource/attachment references, credit accounts
  and credit ledger SHA-256 snapshots matched the pre-migration baseline exactly.
- Backups and release records: `/data1/rag-oss-release-20261106/backup` and
  `cutover.log`. Backup contains source/config, PostgreSQL custom dump and previous
  image identifiers; pre-OSS image tags are retained. Database is at 0058.
- Release includes storage changes and prerequisite inert schema only, retaining
  the existing Docker execution workflow; cloud computer rollout was not included.
- Public home and storage admin routes return 200. Real model, paid OCR and a live
  paid embedding workflow were intentionally not invoked; mocked regressions cover
  those storage integration boundaries. No sandbox mount or preview was added.
