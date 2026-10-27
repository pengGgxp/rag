# Shared model providers and complete pricing templates

Issue: #38. Related settlement work: #36.

## Goal and boundary

Centralize chat model connections in the site console. Workspaces select a published provider and retain independent price copies. Remove runtime chat environment fallback. Preserve historical provider IDs, usage, balances and price snapshots; do not enable request settlement or change commercial prices.

## Implementation

- Global providers own encrypted credentials, connection versions, publication, availability and default model prices. Existing provider rows remain workspace associations.
- Copy model template prices to new global providers, then copy provider prices once when a workspace first selects it. Switching back never replaces prices. OCR and Embedding template prices copy directly to new workspaces.
- Workspace members read sanitized model choices; workspace administrators select; site administrators manage connections and all prices.
- Migrate existing connections individually and preserve their private scope. Import the legacy environment connection only through an idempotent console command. No runtime environment fallback.
- Missing prices remain missing. Draft templates are permitted. New selection/activation requires complete model prices. Existing migrated active connections retain their prices and billing gate.

## Verification and release

Test authorization, scope, secrets, copies, concurrent selection, versions, disabled providers, draft/zero prices, migration idempotency and historical keys. Build the frontend, check the diff, and visually inspect 1440/700/390px with Playwright. Back up source/configuration/database/images, drain model traffic, migrate and validate before recreating API, Web and Worker. Worker must use the same revision because document pricing preflight runs there. Do not rebuild data services. Keep the issue open when real acceptance remains incomplete.

Rollback preserves the added tables, associations and consumption. Do not restore an old API that ignores global providers after new usage is written.

## Release procedure

1. Preserve the existing images with release tags and back up the database, source and configuration in a protected release directory. Record data-service container IDs and the environment-file checksum.
2. Build API, Worker and Web without switching the running containers. Stop incoming API traffic and drain in-flight model/worker requests before taking the final database backup.
3. Run `alembic upgrade head`, then `python -m app.manage models migrate` with the new API image and existing environment. The command imports credentials without printing them and commits only if every active association is valid. A persisted completion marker prevents later workspaces from being accidentally imported on reruns.
4. Repeat the migration command to verify zero changes. Check association counts, preserved policy versions/balances and decryptability before bringing API/Web/Worker up. The old chat environment fields are accepted only by this console importer, never by model execution; keep other environment configuration unchanged.
5. Check health, authenticated settings/admin routes and a controlled model request. Do not enable settlement, invent cache prices, select a new site default or overwrite document prices.

## Local verification, 2026-10-27

- Linux backend: 298 passed, 13 skipped; PostgreSQL concurrency/transaction suite: 5 passed. Fresh migration through revision 0024 succeeded.
- Windows full run identified an existing ZIP fixture difference (backslash normalization); the corresponding Linux security tests passed.
- Frontend production build and diff check passed. Local Playwright API fixtures verified creation, credential non-return, draft OCR zero versus missing Embedding, selection persistence, unsaved-close confirmation, keyboard and Escape/reduced-motion behavior.
- Captured provider form, template and selection screenshots at 1440, 700 and 390 pixels in `output/playwright/issue38-*`; no document-level horizontal overflow. Fixed checkbox sizing and action-row layout discovered through screenshots. Fixtures do not constitute live upstream acceptance.
- Source/configuration migration is additive. Existing missing prices remain missing; new model selection requires complete prices even while migrated active connections continue under the existing billing gate.
