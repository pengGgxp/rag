# CI/CD and production releases

This document is the single operational contract for continuous integration,
production releases, migrations and rollback. Other documents should link here
instead of describing their own deployment procedure.

## Release contract

- Pull requests and pushes to `main` run CI only.
- Production can be released only by pushing an annotated `vX.Y.Z` tag whose
  commit is already on `main`.
- Manual release workflow runs are verification-only. They never deploy.
- The `production` GitHub Environment must require a repository administrator
  approval. Branch protection on `main` must require the CI checks.
- Every production service runs a `linux/amd64` image pinned by
  `image@sha256:...`. The server never builds application images and never
  bind-mounts source code.
- Server-specific secrets stay in protected server files. GitHub stores only
  the SWR credentials, the dedicated deployment SSH key and non-secret values.

## Required GitHub configuration

Repository-level configuration, visible to the tag build before any production
approval:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `SWR_REGISTRY` | Regional Huawei Cloud SWR hostname |
| Variable | `SWR_ORGANIZATION` | SWR organization containing the three repositories |
| Variable | `SWR_REGION` | SWR region used by the cleanup API, for example `cn-south-1` |
| Variable | `NEXT_PUBLIC_API_URL` | Public production API URL, normally `https://zk.str1ct.top/api` |
| Secret | `SWR_USERNAME` | SWR Docker login username |
| Secret | `SWR_PASSWORD` | SWR Docker login password |
| Secret | `SWR_ACCESS_KEY` | Huawei Cloud AccessKey ID used only by the SWR cleanup API |
| Secret | `SWR_SECRET_KEY` | Huawei Cloud AccessKey secret used only by the SWR cleanup API |
| Secret | `SWR_PROJECT_ID` | Huawei Cloud project ID for the SWR region |

Environment `production`, approved before any server access or registry
cleanup:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `PRODUCTION_HOST` | Production SSH hostname |
| Variable | `PRODUCTION_PORT` | Production SSH port |
| Variable | `PRODUCTION_USER` | Dedicated deployment account |
| Variable | `FIRST_TAKEOVER` | `true` only for the single first controlled cutover; return to `false` immediately after |
| Secret | `PRODUCTION_SSH_KEY` | Dedicated private deployment key |
| Secret | `PRODUCTION_SSH_KNOWN_HOSTS` | Pinned host key entry for the production host |

The three SWR repositories are `zhiku-api`, `zhiku-worker` and `zhiku-web`.
Repository permissions should allow CI to push but not delete. The deployment
account needs permission only to run the fixed `sudo` entrypoints and read the
incoming release directory. Do not reuse an administrator's personal SSH key.

## CI gates

The CI workflow runs on every pull request and on `main`:

- Backend tests against PostgreSQL with pgvector, including the migration chain.
- Deployment contract and orchestration tests.
- Frontend unit tests, TypeScript checking and a production build.
- Dockerfile build checks for API, Worker and Web on `linux/amd64`.
- Compose parsing, Actionlint, ShellCheck and secret scanning.
- Manifest, checksum and Nginx package completeness checks.

The Web build requires an explicitly injected `NEXT_PUBLIC_API_URL`. A missing
value, `localhost`, `127.0.0.1` or a non-HTTPS production URL fails the build.

## Immutable release package

The tag workflow publishes three images to SWR and records their immutable
digests. A release archive then contains:

- `release.json`: schema version, release version, commit, build time, three
  image digests, Alembic head, configuration contract and migration rollback
  policy.
- The production Compose file, health checker, migration helpers, Nginx files,
  maintenance client and fixed server entrypoints.
- `SHA256SUMS`, which covers every packaged file except itself.

The archive is uploaded with a separate SHA-256 file. The server verifies both
the outer archive checksum and the inner package checksum before executing any
release script. A versioned incoming directory is single-use; retry with a new
incoming directory rather than replacing files in place.

The GitHub artifact and server release directory are the release evidence.
Keep the tag, commit, digests, Alembic versions, backup checksum, health result
and rollback result together.

## Production filesystem

```text
/opt/zhiku/
  app.env -> config/app.env
  config/
    app.env                         # root-only application secrets, mode 600
    .env.question-workbench         # root-only bridge secrets, mode 600
  bin/
    deploy-release
    health
    maintenance
    prune-images
    rollback
    run-release
  incoming/<version>/               # uploaded archive, never reused
  releases/<version>/               # immutable extracted release and evidence
  current -> releases/<version>
  previous -> releases/<previous-version>
  deploy.lock
```

`app.env` is the original production application environment. It must contain
at least:

```dotenv
DATABASE_URL=postgresql+psycopg://...
REDIS_URL=redis://...
AUTH_SECRET_KEY=...
SITE_CONFIG_ENCRYPTION_KEY=...
MCP_CREDENTIAL_KEY=...
NEXT_PUBLIC_API_URL=https://zk.str1ct.top/api
FRONTEND_URL=https://zk.str1ct.top
API_PUBLIC_URL=https://zk.str1ct.top/api
AUTH_COOKIE_SECURE=true
PREVIEW_ENABLED=true
PREVIEW_DOMAIN=preview.str1ct.top
DEPLOY_CONTROL_TOKEN=...
```

`DEPLOY_CONTROL_TOKEN` must be at least 32 characters. It is used only by
server-side processes calling the loopback maintenance endpoint. Never send it
to the browser, print it, put it in GitHub, or pass it in a command argument.
During first takeover the release script preserves the token in the protected
server environment so the running API and later releases use the same value.

`.env.question-workbench` remains a separate protected file. The release
process copies both protected files into each immutable release with mode
`600`. `.env.release` contains only Compose interpolation values; it is not an
application secret file.

## First production takeover

Do not treat a deployed release as a completed takeover. Follow these stages:

1. Inventory the running containers, effective Compose chain, images, volumes,
   network, Nginx files, environment-file mode and current Alembic revision.
2. Confirm both protected environment files exist. Do not seed from an empty
   database, alternate network or newly created volume.
3. Push the approved semver tag with `FIRST_TAKEOVER=true`.
4. The workflow verifies and uploads the archive, then installs the fixed
   server entrypoints and protected configuration copies. This bootstrap step
   does not start containers, alter Nginx or change the database.
5. The shadow stage verifies package checksums, renders Compose and checks the
   packaged migration heads without switching `current`.
6. Obtain the production Environment approval and perform one controlled
   `takeover` action.
7. A temporary Nginx gate blocks new writes before legacy producers are
   stopped. The script backs up the database, verifies the backup copy, restores
   it into an isolated rehearsal database and runs the migration there.
8. Only after rehearsal succeeds does it migrate production, start the digested
   release, run health checks, install canonical Nginx and remove the temporary
   maintenance gate.

Set `FIRST_TAKEOVER=false` immediately after the first successful cutover.
Subsequent releases use the normal managed-release path.

## Normal tag release

1. Merge the change to `main` with green CI.
2. Create an annotated semver tag on that exact commit and push the tag.
3. GitHub verifies the tag regex, commit identity, `main` ancestry and clean
   checkout.
4. CI tests must pass. The release workflow builds and pushes the API, Worker
   and Web images, records digests and creates the release package.
5. Approve the `production` Environment deployment.
6. The server verifies the uploaded archive, obtains the deployment lock, turns
   maintenance on through the currently running API, stops Workers, drains
   finite work, backs up and validates the database, rehearses migrations in an
   isolated database and then follows the migration path below.
7. If the release is healthy, evidence is written and maintenance is cleared.
8. Only after a successful release, and after the `production` Environment
   approval, may cleanup remove unneeded SWR tags and local images. During
   first takeover, the archived legacy `previous` directory may not contain a
   `release.json`; only the current release manifest is mandatory.

Never manually edit a production Compose file, append a temporary override or
replace a service image on the server. Fix the release and produce a new tag.

## Migration rules

Application images never run `alembic upgrade head` at startup. A one-off
migration job uses the same digest-pinned API image.

- With no pending migration, changed services may roll normally; Workers are
  still drained before replacement.
- With a pending migration, the sequence is: maintenance on, finite Worker
  drain, custom-format database backup, checksum and copy verification,
  isolated database restore, `alembic upgrade head` rehearsal against the
  release head, formal migration, release start and full health checks.
- Rehearsal failure leaves the production database untouched and exits
  maintenance only according to the documented failure path. It never runs a
  production migration.
- A formal migration failure does not start the new application. Keep
  maintenance active and use the recorded backup/restore command under
  administrator control. Never automatically downgrade.
- `automatic-app-rollback` means the migration is compatible with the previous
  application; a failed health check may automatically restore the previous
  release and image digests.
- `restore-backup-required` means application rollback is forbidden until the
  verified database backup is restored. Keep maintenance on and involve an
  administrator.

## Maintenance state

The API exposes loopback-only `GET` and `PUT
/internal/deploy/maintenance`, protected by `DEPLOY_CONTROL_TOKEN`. External
Nginx rejects `/internal/`; the endpoint is not a public administration path.

During maintenance, reads, health checks, payment callbacks, order status and
the explicit stop/cancel controls remain available. New conversations, uploads,
resource writes, new paid orders, cloud-computer starts and other new paid
work are rejected with `503` and `Retry-After`.

The state is persistent in PostgreSQL. A failed or interrupted deployment does
not silently clear it. Use the release evidence and the fixed scripts to finish
or recover, then clear maintenance deliberately.

## Health checks and acceptance

A release is healthy only when all applicable checks pass:

- API `/ready` reports an `ok` database and Redis, and a fresh Worker heartbeat.
- API `/version` matches the release version, commit, configuration contract
  and rollback policy.
- The public API health endpoint and Web home page are reachable.
- Resource library and checkout pages return successfully.
- Preview gateway health is available.
- The database Alembic revision equals the release manifest head.
- The question-workbench bridge health is reachable from the API network when
  it is configured; otherwise the result is explicitly `skipped`, not `ok`.
- `nginx -t` passes before hot reload.

Container running alone is not Worker health. A missing or expired Redis
heartbeat fails the release. Repeated failures, 5xx responses, digest mismatch
or Alembic mismatch also fail closed.

## Rollback and recovery

For a managed compatible release:

```sh
sudo /opt/zhiku/bin/health
sudo /opt/zhiku/bin/maintenance status
sudo /opt/zhiku/bin/rollback <failed-version>
```

`rollback` switches `current` back to `previous`, pulls the previous digests,
starts the previous Compose release and repeats health checks. Never delete the
current or previous release directory or their images before a later release
is verified.

For `restore-backup-required`, do not run application rollback:

1. Keep maintenance active and stop new writers.
2. Read `evidence.json` and verify `<backup>.sha256`.
3. Restore the recorded custom-format PostgreSQL backup to the production
   database using the documented administrator command.
4. Check balances, ledgers, orders, documents, resource indexes and object
   references before resuming writes.
5. Only then switch `current` to the previous release and clear maintenance.

Routine troubleshooting:

- Health timeout: inspect release evidence, `nginx -t`, service logs and the
  actual digest. Do not change a tag or override an image.
- Duplicate deploy: the lock rejects concurrency. Wait for the active process;
  do not remove the lock manually.
- Interrupted upload: use a new incoming version directory and rerun the
  verified archive upload.
- Nginx failure: the script restores the prior Nginx files and reloads them;
  if maintenance remains active, resolve it explicitly.
- Worker drain timeout: keep maintenance active and inspect active tasks. Do
  not start another release concurrently.
- Registry/retention issue: preserve current and previous digests and release
  directories. Registry cleanup must never delete the rollback digest.

## Retention

Server release directories retain the current and previous immutable packages.
Older directories may be archived only after a later release is verified and
the rollback chain is no longer needed. Local Docker cleanup removes only
dangling images and build cache; current and previous referenced images stay.
SWR tag/digest retention is likewise conservative: never delete the current or
previous digest, and never make deployment depend on the cleanup job.

## Evidence to retain

For each release retain:

- Release tag, commit SHA, package SHA-256 and SWR image digests.
- Current and previous Alembic revisions and migration rollback policy.
- Database backup path, byte size and SHA-256.
- Pre-change and canonical Nginx files.
- Health results, maintenance state changes and final release status.
- Application and database rollback commands.
