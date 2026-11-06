# Object storage: COS and OSS

## Configuration and API

`/admin/storage` keeps separate Tencent COS and Alibaba OSS connection tabs.
Only one connection is active. Saving and testing never switch the active store.
Credentials use `SITE_CONFIG_ENCRYPTION_KEY`; API and Worker need the same key.
Blank values retain credentials, explicit clear removes them only when inactive.
Active locations cannot be edited, and active credential rotation is tested before
committing. Conflict responses retain the administrator's input.

- `GET /site/admin/storage`: `{active_provider, connections: [...]}`. Each entry
  contains `provider`, independent `version`, bucket/region, masked ID, configured
  flags, active/tested status and safe test result. No plaintext credentials.
- `PUT /site/admin/storage`: `{provider, version, bucket, region, secret_id?,
  secret_key?, clear_credentials?}`. `provider` is `cos` or `oss`; returns the
  selected connection. OSS uses AccessKey ID/Secret in the shared credential fields.
- `POST /site/admin/storage/test`: `{provider, version}`. Creates and removes
  isolated `_checks/` objects; validates ordinary and multipart operations, abort,
  range, HEAD and listings. It does not modify bucket configuration or activate it.

OSS uses the official `alibabacloud-oss-v2==1.4.1` SDK and a region-derived HTTPS
endpoint. Use a private bucket, preferably `cn-hangzhou` for the existing sandbox
region. This integration does not mount storage into cloud computers or change
object layout. Existing storage/download billing and object-key dedup stay intact.

## Dedicated RAM permissions and CORS

Use a dedicated RAM identity scoped to the selected bucket, not account-wide keys.
Required bucket actions: `oss:ListObjects`, `oss:ListMultipartUploads`.
Required object actions: `oss:GetObject`, `oss:PutObject`, `oss:DeleteObject`,
`oss:ListParts`, `oss:AbortMultipartUpload`. UploadPart, initiation and completion
use the PutObject authorization. Check the current official action mapping when
setting the policy. No bucket creation, public ACL, lifecycle or CORS mutation is
performed by the app. Anonymous reads must remain denied.

Configure exact browser origins in the OSS console, production
`https://zk.str1ct.top`, allowed method `PUT`, allowed headers `Content-Type` and
`Content-Length`, exposed header `ETag`, max-age 600 seconds. Add explicit local
origins only if needed; do not grant anonymous reads or use wildcard origins.
Connection tests run server-side and do not prove browser CORS readiness.

SDK V2 1.4.1 does not propagate additional signed headers into its presign context.
The adapter uses the public OSS V4 signer to bind Content-Length on part URLs;
the bucket, object key, upload ID, part number and 15-minute expiry remain bound.
The browser supplies the body length automatically. Whole-file SHA-256 and actual
part sizes are still independently checked before document admission.

## Offline cutover procedure

1. Back up the database, configuration, source and API/Worker/Web image references.
   Record current provider and versions. Keep all COS objects and credentials.
2. Deploy the coordinated adapter/schema/UI update while COS remains active. Save
   OSS settings and pass connection tests, then perform real isolated-prefix
   upload, range, multipart, private-access and browser CORS checks.
3. Pause admission of new paid work, finish/cancel pending uploads and drain
   ingestion, conversations, image tasks and cloud sessions. Stop API, Worker,
   executor and any other resource writers/cleanup jobs before running migration
   in a one-off container. Keep DB and object storage available. A stopped Web or
   reverse-proxy maintenance response prevents clients queuing new mutations.
4. Preview with the reviewed OSS configuration version:

   ```sh
   python -m app.migrate_storage --target oss --version N
   python -m app.migrate_storage --target oss --version N --execute --offline
   ```

5. The manifest includes every document version, including soft-deleted originals,
   retained available resource versions and attachments whose physical objects
   have not been deleted. References to the same key are deduplicated; disagreeing
   metadata blocks execution. Known unavailable/deleted physical resources are not
   restored. No bucket-wide copy or orphan import takes place.
6. Each object is streamed to temporary disk, source SHA-256/size checked, copied
   under the same key/type, and target downloaded for checksum verification.
   Existing target objects must match or execution stops without overwriting.
   A second full source/target verification and manifest/config check precede the
   atomic provider switch. Each completed object's progress is durable. No user
   ledger entries are created for operator migration traffic.
7. On failure the source stays active and the error returns the migration ID:

   ```sh
   python -m app.migrate_storage --target oss --version N --execute --offline --resume UUID
   ```

   The same configs and manifest must still apply. Already-present target objects
   are reverified, not blindly trusted. Credentials/config edits require a new run.
8. On success both connection versions advance, invalidating stale admin forms.
   Start API and Worker, verify authenticated old-file downloads and a synthetic
   new resource upload/download/delete before restoring public writes. Verify
   document processing with local text/mocked embedding unless paid validation
   has been explicitly authorized. Historical resource/document URLs are unchanged.

The CLI holds a session advisory lock on a pinned PostgreSQL connection across
per-object commits, preventing concurrent config saves/other CLI migrations.
`--offline` is an operator acknowledgement, not a substitute for stopping writers.
The CLI also refuses undrained task records. A crash does not switch providers;
restart with the printed run ID. Existing temporary parse checkpoints are not
persistent resources; finish/retry or cancel those jobs before cutover.

## Rollback

Before restoring user writes, rollback may use the same migration path toward COS.
After any OSS writes, stop/drain writers again and run a reverse migration with
the current tested COS version. It includes new OSS objects and verifies them
before switching. A source-image-only rollback after new writes is unsafe.
The source objects are never automatically deleted, and no permanent dual-write
or read-fallback path is introduced. The old `migrate_cos` command is only for the
historical legacy-S3 bootstrap and must not be used for COS/OSS cutover.

## Validation status

See `docs/tasks/oss-storage.md`. Mocked browser/adapter tests do not establish real
OSS permissions, CORS, network performance or successful production migration.
