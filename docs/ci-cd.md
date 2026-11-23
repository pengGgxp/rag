# CI/CD and production releases

This document is the single operational contract for continuous integration,
production releases, migrations and rollback. Other documents should link here
instead of describing their own deployment procedure.

The test environment uses a separate `test` branch and manually invoked local
image builds and deployment. It does not use GitHub Actions for publishing or
deploying test images. See [the test release runbook](../deploy/testing/README.md).

Current public endpoints:

- Production: `https://syfc.moonlitstarway.top/`
- Test: `https://zk.str1ct.top/`

## Release contract

- Production configuration is server-owned. The authoritative files are
  `/opt/zhiku/config/app.env`, the protected question-workbench env file and
  the generated release `.env.release`.
- GitHub Actions builds and publishes production images and assembles the
  release package. The operator starts deployment on the production server.
- Every production service runs a `linux/amd64` image pinned by
  `image@sha256:...`. The server never builds application images and never
  bind-mounts source code.
- The machine-readable variable contract is maintained in
  `deploy/app.env.example`, `deploy/deployment.env.example` and
  `deploy/production.env.reference.md`. CI runs
  `python deploy/validate_config.py` before packaging.
- Branch protection on `main` should require CI checks.

## GitHub release settings

The release workflow uses these repository settings to push to SWR.

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `SWR_REGISTRY` | Regional Huawei Cloud SWR hostname |
| Variable | `SWR_ORGANIZATION` | SWR organization containing the three repositories |
| Secret | `SWR_USERNAME` | SWR Docker login username |
| Secret | `SWR_PASSWORD` | SWR Docker login password |

The three SWR repositories are `zhiku-api`, `zhiku-worker` and `zhiku-web`.
CI needs permission to push to those repositories. The production server
uses its own registry login to pull the images. The Web build uses
`https://syfc.moonlitstarway.top/api`.

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

`app.env` is the original production application environment. The complete
field-by-field Chinese index is `deploy/production.env.reference.md`; the
copyable template is `deploy/app.env.example`. The index marks which settings
are required, capability-specific, optional, legacy or generated.

```dotenv
DATABASE_URL=postgresql+psycopg://...
REDIS_URL=redis://...
AUTH_SECRET_KEY=...
SITE_CONFIG_ENCRYPTION_KEY=...
MCP_CREDENTIAL_KEY=...
NEXT_PUBLIC_API_URL=https://syfc.moonlitstarway.top/api
FRONTEND_URL=https://syfc.moonlitstarway.top
API_PUBLIC_URL=https://syfc.moonlitstarway.top/api
AUTH_COOKIE_SECURE=true
PREVIEW_ENABLED=true
PREVIEW_DOMAIN=preview.str1ct.top
DEPLOY_CONTROL_TOKEN=...
```

`DEPLOY_CONTROL_TOKEN` must be at least 32 characters. It is used only by
server-side processes calling the loopback maintenance endpoint. Never send it
to the browser, print it, put it in GitHub, or pass it in a command argument.
`NEXT_PUBLIC_API_URL` is checked against the Web build input for release
consistency; changing it does not rebuild an existing Web image.
During first takeover the release script preserves the token in the protected
server environment so the running API and later releases use the same value.

`.env.question-workbench` remains a separate protected file. The release
process copies both protected files into each immutable release with mode
`600`. `.env.release` contains only Compose interpolation values; it is not an
application secret file. `deploy/deployment.env.example` documents that layer,
but the actual `.env.release` is generated from `app.env` and the release
manifest rather than entered twice.

Both API and the ordinary Worker must load `.env.question-workbench`: API
authorizes selection, while the Worker supplies and executes the model tools.
Release health checks compare their effective bridge configuration without
logging values and verify connectivity from both containers. A healthy API
alone does not verify the background conversation tool path.

## First production takeover

Do not treat a deployed release as a completed takeover. Follow these stages:

1. Inventory the running containers, effective Compose chain, images, volumes,
   network, Nginx files, environment-file mode and current Alembic revision.
2. Confirm both protected environment files exist. Do not seed from an empty
   database, alternate network or newly created volume.
3. Push the approved semver tag.
4. Download the Actions artifact, upload it to the server and verify its
   checksum. Run the packaged bootstrap script to install the server entrypoints.
5. The shadow stage verifies package checksums, renders Compose and checks the
   packaged migration heads without switching `current`.
6. Invoke the server entrypoint with the `takeover` action.
7. A temporary Nginx gate blocks new writes before legacy producers are
   stopped. The script backs up the database, verifies the backup copy, restores
   it into an isolated rehearsal database and runs the migration there.
8. Only after rehearsal succeeds does it migrate production, start the digested
   release, run health checks, install canonical Nginx and remove the temporary
   maintenance gate.

Subsequent releases use the normal managed-release path.

## Normal tag release

Every managed release checks the payment proxy topology before maintenance or
migration, and checks the installed API/Nginx configuration before acceptance.
Empty/stale gateway trust, an exposed API port or an unexpected transport peer
blocks the release even when `/health` succeeds. The check never adds observed
sources to trust. See [payment proxy checks and recovery](payment-client-ip.md).

1. Merge the change to `main` with green CI.
2. Create an annotated semver tag on that exact commit and push the tag.
3. GitHub verifies the tag format, commit identity and `main` ancestry.
4. CI tests must pass. The release workflow builds and pushes the API, Worker
   and Web images, records digests and creates the release package.
5. Download the release artifact, transfer it to the server and run the
   fixed server entrypoint.
6. The server verifies the uploaded archive, obtains the deployment lock, turns
   maintenance on through the currently running API, stops Workers, drains
   finite work, backs up and validates the database, rehearses migrations in an
   isolated database and then follows the migration path below.
7. If the release is healthy, evidence is written and maintenance is cleared.
8. After a successful deployment, the operator may clean up unused SWR tags
   and local images while keeping the current and previous release images.

Never manually edit a production Compose file, append a temporary override or
replace a service image on the server. Fix the release and produce a new tag.

Application image availability is checked before maintenance: cached immutable
digests are reused, and only missing application images are pulled from SWR.
Starting services uses `--pull never`, so an application rollout or rollback
does not refresh unchanged database, Redis or object-storage images from Docker
Hub. Keep the current and previous images, including their infrastructure images,
on the host; a missing infrastructure image must be prepared before rollout.

## Publish and deploy a release

Choose an unused version and the exact commit from `main` to release.

```sh
git fetch origin main
release_commit=$(git rev-parse origin/main)
release_version=vX.Y.Z
git tag -a "$release_version" "$release_commit" -m "Release $release_version"
git push origin "refs/tags/$release_version"
```

Wait for the `release` workflow for that tag to succeed. Download its
`production-release-<version>` artifact using the run ID shown in Actions.

```sh
gh run watch "$run_id" --exit-status
gh run download "$run_id" \
  --name "production-release-$release_version" \
  --dir "release-$release_version"
```

Upload `release-$release_version/zhiku-$release_version.tar.gz` and its
`.sha256` file to a new `/opt/zhiku/incoming/$release_version/` directory.
Log into the server and run the release entrypoint. It reads the image
digests from the verified package and pulls them directly from SWR.

```sh
# Server preflight and controlled release.
cd "/opt/zhiku/incoming/$release_version"
sudo sha256sum -c "zhiku-$release_version.tar.gz.sha256"
sudo /opt/zhiku/bin/deploy-release \
  "/opt/zhiku/incoming/$release_version" deploy
```

Run the health command using the version and Alembic values from the release
manifest; do not copy them from an older release log:

```sh
sudo /opt/zhiku/bin/health \
  --expected-version "$release_version" \
  --expected-commit "$release_commit" \
  --expected-alembic "$alembic_head"
```

### Server preflight

Before allowing the server to execute anything:

1. Confirm the archive and outer checksum match.
2. Confirm the inner `SHA256SUMS` match and the extracted scripts are present.
3. Read the current release manifest and record its version, commit, digests
   and Alembic revision.
4. Confirm the protected application environment files exist with mode `600`
   and that their configuration contract still matches.
5. Confirm the deployment lock is not held and inspect active Worker work.
6. Confirm the current and previous release directories and images are
   retained as rollback candidates.

The server must not seed a release from an empty database, a new volume or an
unexpected Compose project. If the current runtime does not match the recorded
release, stop and reconcile it manually before changing anything.

### Migration decision

Read the release manifest before deployment. If the current Alembic revision
already equals its head, the release has no pending migration and may use the
normal rolling replacement path. If they differ:

1. Turn maintenance on through the currently running API.
2. Stop the Workers from accepting new finite work and drain them.
3. Create a custom-format PostgreSQL backup.
4. Verify the backup checksum and a copy of the backup.
5. Restore the backup into an isolated rehearsal database.
6. Run `alembic upgrade head` there and require the exact release head.
7. Only then migrate production with the one-off API-image job.

Never run a production migration merely because the rehearsal failed to start;
never auto-downgrade. For `restore-backup-required`, stop after any failed
release check and keep maintenance active until the verified backup is
restored.

### Switch and health

1. Stop new producers and wait for finite Worker tasks to drain.
2. Switch `current` to the immutable release directory.
3. Start only the services whose image or configuration changed, plus the
   required one-off migration job. Compose does not wait for container health.
4. Verify running container images contain the expected digests.
5. Verify the database Alembic revision and run `nginx -t` before reloading
   the active configuration. These are deployment-integrity checks, not the
   application health suite.
6. Clear application maintenance. On first takeover, install the canonical
   Nginx configuration to remove its temporary maintenance gate.
7. Only after maintenance is clear, run the packaged health checker with the
   expected version, commit and Alembic revision. It verifies the API, Web,
   resource library, checkout page, preview gateway, Worker heartbeat and
   configured question-workbench bridge.
8. Write release evidence, including maintenance changes, backup checksum,
   Nginx backup and the final health result.

Do not use browser cookies from a real user to test a controlled deployment.
Use a test account, unauthenticated status endpoints or an isolated test
workspace, and never put user content or credentials in release evidence.

### Failure handling

- Build or registry failure: retain the failed logs, use a new version, and
  do not alter the running release.
- Package or checksum failure: stop before switching and upload a new package.
- Rehearsal failure: leave production unchanged, keep maintenance active and
  inspect the rehearsal database and migration output.
- Compatible application failure: re-enable maintenance, switch back to the
  previous release and digests, clear maintenance on that release, then run
  its health checks. If rollback verification fails, re-enable maintenance
  and leave the previous release available for diagnosis.
- `restore-backup-required` failure: keep maintenance active, restore the
  verified database backup and reconcile balances, ledgers, orders, documents,
  resource indexes and object references before switching application code.
- Interrupted release: inspect the lock, current link, running digests,
  maintenance marker and evidence file. Do not assume the process completed
  because containers are running.

### Current production state and legacy evidence

The managed release root is `/opt/zhiku`; `current` and `previous` point to
immutable release directories. Historical releases created before the managed
root were stored under `/data1/rag-releases/<release-name>` and remain useful
as migration and rollback evidence. Do not treat every historical directory
as a currently deployed release.

As of 2026-11-11, the active managed release is `/opt/zhiku/releases/v0.1.0`
at commit `d74f50e62995bb57225f0fd946f651cc5d3a2134`, with Alembic head
`0063_deployment_maintenance`. Its API and Worker digests are identical and
its Web digest is distinct. The release manifest is the authority for the
digests; any later document or dashboard must not infer them from a container
name.

The Actions run records publication. The server release directory records
deployment, including the health and backup evidence.

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
- The question-workbench bridge has matching effective API/Worker configuration
  and is reachable from both containers when configured; otherwise the result
  is explicitly `skipped`, not `ok`.
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

Before changing `current`, rollback validates the saved previous configuration
against the current Docker network. A network rebuild may therefore block an
old snapshot. Back up and explicitly correct its gateway setting as described
in [payment proxy recovery](payment-client-ip.md); do not bypass the check with
an older release script or broaden the trusted range.

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
