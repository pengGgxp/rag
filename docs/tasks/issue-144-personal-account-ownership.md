# Issue #144: Remove Multi-User Workspaces

## Goal

Move application authorization and business ownership from shared workspaces to
individual user accounts. Preserve site-administrator capabilities without
promoting former workspace administrators.

## Scope

- Remove workspace switching, membership, invitation, role, and knowledge-base
  member-permission APIs and UI.
- Scope application data, files, jobs, previews, billing, model policy, and
  audit history to users.
- Migrate existing workspace data to a deterministic receiving user, preserving
  historical billing values and object keys.
- Physically remove suspended test accounts and their associated database,
  object-storage, and audit data through a retryable maintenance command.
- Keep shared model providers, credentials, and baseline prices site-managed.

## Out Of Scope

- Adding an account self-deletion workflow.
- Rebuilding vectors or changing document version semantics.
- Implementing knowledge-base synchronization from issues #138 or #141.
- Running destructive migration or account cleanup against a live environment
  as part of code implementation.

## Acceptance

- Each account can only read, mutate, retrieve, download, or use its own
  resources, including through guessed resource IDs and background tools.
- Workspace collaboration APIs return not found and no workspace/member/role
  controls remain in user-facing or site-admin interfaces.
- Migration ownership selection is deterministic and fails preflight when no
  administrator can receive a workspace.
- Merged account balances and allowances are additive; available models are
  unioned; default model and multiplier come from the earliest workspace;
  historical usage and bills are moved without repricing or duplicate posting.
- File path conflicts preserve both objects and resolve to distinct names.
- Suspended-account cleanup is repeatable, separates database and object-store
  retries, and reports remaining object keys without retaining deletion audits.
- Normal-account audit history remains queryable by user.
- Backend tests, PostgreSQL migration tests, frontend type checks/build, and
  1440/700/390px UI checks pass.

## Verification

Run focused backend isolation and migration tests first, then the full backend
suite. Run PostgreSQL migration and cleanup retry tests against isolated test
services only. Run frontend tests, type checking, production build, and browser
checks at the three required widths. Do not execute destructive operations
against production data during implementation.

### Verification results

- Backend regression suite passed with 1,450 tests. PostgreSQL and other
  opt-in tests are reported separately.
- All 84 PostgreSQL tests passed against disposable databases. They covered
  the ownership migration from revision `0078`, decimal billing, pending
  jobs, payments, concurrent access, and cleanup retries.
- The maintenance command deleted a suspended account, its audit records,
  and objects under both account and legacy prefixes in a local S3-compatible
  store. Normal-account records and objects remained, and a second invocation
  found no cleanup work.
- All 55 frontend tests, TypeScript checking, and the production build passed.
  Browser checks used the running local API and covered point adjustments,
  storage grants, and user details, resources, and knowledge bases at 1440,
  700, and 390 pixels.
- Cloud tests covered legacy mount handoff, shared resource visibility,
  current object keys for previews, and concurrent cloud jobs. The paid
  Alibaba sandbox and live OSS mount were outside this local test run.

### Migration operation

Stop old API and worker processes, back up the database, and apply
`alembic upgrade head`. Resume the deployment with the matching new code.
Run `python -m app.manage personal-accounts cleanup-suspended` with the same
database and object storage configuration before reopening traffic. Repeat
the command after storage errors until the reported object queue is empty.
Restoring the pre-migration database backup is required to reverse the schema
transfer. Object deletions are permanent.

The migration preserves the old site-wide catalog for existing users and uses
the site default when the earliest workspace has no active binding. Legacy file
keys remain in the database. Before starting a new cloud computer, its account
must finish any existing instance mounted to a legacy prefix. Current files
then become available under the account mount, with previews and downloads
reading the mounted key. The question workbench service keeps its historical
storage namespace and validates the session and account on each request.
