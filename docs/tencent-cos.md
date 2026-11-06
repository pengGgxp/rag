# Tencent COS Storage And Uploads

COS/OSS configuration and current verified provider cutover procedure are documented
in [Object storage: COS and OSS](oss-storage.md). The migration command below is the
historical legacy-S3 bootstrap only; do not use it to switch between COS and OSS.

Issue: #46. Target bucket: `zk-1309923787`, region `ap-guangzhou`.

## Scope And Invariants

- Private bucket, official Python SDK, browser-native multipart PUT. No CDN,
  global acceleration, CI or OCR pricing changes.
- Maximum original file: 200 MiB (209715200 bytes). Parts: 8 MiB, concurrency
  three, signed part URLs: 15 minutes, sessions: 24 hours.
- Only the backend initializes, completes and aborts multipart uploads. The
  signed PUT binds the key, upload ID, part number and exact content length.
- Worker admission checks the account, membership and library write permission,
  actual size, file header, per-part SHA-256 and whole-file SHA-256. A multipart
  ETag is never treated as the final file digest.
- Library row locking serializes deduplication and version allocation. Upload
  row locking makes repeated completion and verification idempotent.
- Original documents, including soft-deleted versions, are not garbage-collected.
  Restore, citations, billing and historical identifiers remain unchanged.

## Configuration

Use `/admin/storage`. Credentials are encrypted with the existing
`SITE_CONFIG_ENCRYPTION_KEY`. The worker must receive the same key as the API.
Do not place COS credentials in `.env`, Git, browser storage or chat.

Use a dedicated CAM sub-user restricted to this bucket, not root credentials.
Required data actions: `GetBucket`, `ListMultipartUploads`, `ListParts`,
`InitiateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`,
`AbortMultipartUpload`, `PutObject`, `HeadObject`, `GetObject`, `DeleteObject`.
Bucket creation, ACL, policy, CORS and lifecycle modification permissions are not
needed by the application.

Save and test the configuration. The test uploads and reads independent test
objects, performs multipart completion, lists objects/uploads and cleans up.
It does not change bucket configuration or establish browser CORS readiness.
Blank credential fields preserve existing values. Clear is explicit and blocked
while storage is active. Version conflicts require reloading, not overwriting.
Bucket and region cannot be changed once active.

### CORS: Confirm Before Cloud Changes

Proposed production rule: origin `https://zk.str1ct.top`, method `PUT`,
allowed headers `Content-Type, Content-Length`, exposed header `ETag`,
max age 600 seconds. Add exact development origins only when needed and approved.
Do not use wildcard origins, anonymous access or credentials in browser requests.
The API does not automatically change CORS or lifecycle configuration.

References:
- https://cloud.tencent.com/document/product/436/8279
- https://cloud.tencent.cn/document/product/436/35059
- https://intl.cloud.tencent.com/document/product/436/31544?lang=en

## Original File Migration

Back up the database, configuration, source and API/Web/Worker images first.
Pause new uploads and paid tasks; drain running ingestion and model requests.
Keep the original object store and its volume.

```sh
docker compose exec -T api python -m app.migrate_cos
docker compose exec -T api python -m app.migrate_cos --execute --version N
```

The preview reports counts, bytes and the tested configuration version. Execution
copies every original object under the existing key, checks source metadata and
downloads the COS copy to compare SHA-256 and size. Only after every object passes
does one transaction enable COS. Repeating an already completed migration is a
no-op. Failures retain the old read source and original objects.

Before activation, old originals remain readable for migration and existing jobs.
New uploads require ready COS; there is no silent legacy upload fallback.
After activation all reads use COS, with no fallback on failures. Startup never
creates a bucket or requires a working cloud connection to expose the admin UI.

## Upload And OCR Operations

- `POST /knowledge-bases/{id}/uploads`: create a user-scoped session.
- `GET /uploads/{id}`: cloud-confirmed parts and durable admission/processing state.
- `POST /uploads/{id}/parts/{n}`: register SHA-256 and sign that part only.
- `POST /uploads/{id}/complete`: returns 202; verification happens in the worker.
- `DELETE /uploads/{id}`: cancels before admission. Admitted files use document
  management, not upload cancellation.
- API startup and hourly maintenance requeue unadmitted verification and queued
  admitted jobs, expire sessions, abort abandoned multipart uploads and remove
  platform-shaped orphan objects older than 24 hours.
- Browser refresh keeps only upload IDs, file metadata and fingerprints. Users
  reselect the original file; cloud-confirmed parts are fingerprint-checked
  before reuse. Workspace changes pause transfer, not verification already sent.
- The worker passes the already downloaded content directly into parsing.
- Aliyun DocMind receives a server-generated, one-hour private COS URL. Oversize
  submissions use existing page/byte batches under `_ocr/`. Task IDs and unknown
  submission checkpoints are preserved. No URL failure silently resubmits bytes.
- Batch objects are removed after results are saved; old `_ocr/` objects are also
  cleaned after 24 hours. Source documents and page/chunk locations are retained.

## Acceptance Status

Local tests use a disposable database and a COS-shaped HTTP fixture. These prove
upload ordering, progress, pause, retry, resume validation and admission, not
Tencent Cloud performance or commercial OCR correctness.

### Live Verification: 2026-10-29

- The upload rejection was caused by tested credentials with storage still
  inactive. A successful connection test is not evidence of a working upload.
- Backed up the database, configuration and image references; confirmed no active
  conversations, ingestion jobs or uploads. Copied the three original documents
  (30,948,283 bytes), checked their sizes and SHA-256 hashes, then activated COS.
  Every original was read again through the application's active storage path.
  Original metadata, billing settings, balances and multipliers were unchanged.
- Existing CORS permits the production origin. No bucket policy, CORS, `.env` or
  Compose configuration was changed. API and Worker are running.
- Playwright uploaded a disposable TXT through the live site and real COS.
  The upload completed verification, parsing and embedding; its document was
  ready with one indexed chunk. The controlled embedding used 43 tokens and one
  credit. The acceptance library was archived and the temporary session revoked.
- Visually checked screenshots at 1440px and 390px:
  `output/playwright/issue46-live-upload-ready-1440.png` and
  `output/playwright/issue46-live-upload-ready-390.png`.

Real 200 MiB/200 MiB+1 byte boundary tests, cancellation/network recovery and a
small DocMind -> retrieval -> citation test remain pending. Do not automatically
submit the 200 MiB transfer test for paid OCR. Issue #46 remains open.
