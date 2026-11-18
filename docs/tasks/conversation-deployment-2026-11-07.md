# Conversation management deployment, 2026-11-07

User authorized push and production deployment after local acceptance.

- Application revision: `35b3dd0`, branch `codex/resource-library`.
- Release directory: `/root/rag-releases/chat76-35b3dd0`.
- API/worker/preview image: `rag-platform-api:chat76-35b3dd0`.
- Historical note: the dedicated server-side conversion image from this release belonged to a retired path and is no longer part of the deployment contract.
- Web: `rag-platform-web:chat76-35b3dd0`, production API URL, `.next-chat76-release`.
- Database upgraded from `0061_resource_previews` to `0062_conversation_management`.

## Backup and switch

The release directory contains a private `backup/` with a verified custom-format PostgreSQL dump, configuration files, effective Compose overlays and previous image references. Retain these files privately; do not commit credentials. Active/reserved Celery tasks and active conversation requests were empty before the switch. Old application services were stopped before migration; new services started only after migration and billing verification succeeded.

Preserve the entire existing Compose overlay chain, including HTTP preview, resource preview, image specs, chat image and chat UI overlays, and append this release's `release.yml`. Do not restart using only the base Compose file. The deployment script and effective configuration are retained on the server.

The small server could not comfortably install/build frontend dependencies. The frontend was built locally with the production API URL and packaged into the existing Linux runtime image. No Windows node_modules were copied. The PDF worker asset is included.

## Production verification

- Public page and API/database/Redis health returned 200; unauthenticated conversation listing returned 401.
- Migration reports `0062_conversation_management (head)`.
- Workers register `execute_conversation` and `name_conversation` and reach ready state.
- Account balances, credit ledger and model price row counts/content hashes, plus message count, were unchanged across migration.
- A temporary isolated account/workspace exercised public authenticated create/list/detail, rename, pin, stale-revision conflict, literal special-character search and deletion. Synthetic records were removed afterward.
- Production browser assets contain no local acceptance API URL; PDF worker responds 200.

The 256 related local backend tests, PostgreSQL tests, frontend build and reviewed 1440/700/390 browser screenshots remain the functional acceptance evidence. Live model generation, image generation/editing and cloud execution were not invoked during deployment; their real-provider workflows remain unverified by this release.

## Recovery

Before reverting application images, stop new requests and drain new executions. Keep execution and billing records; never replay uncertain model/tool operations. The preceding overlay chain and image references are preserved in the backup. Migration is additive; do not downgrade or restore the database over newer user data without an explicit recovery decision. If restoring a backup is required, coordinate a write pause and account for all writes after the backup.
