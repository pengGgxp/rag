# GHCR template creation and cutover

Goal: create and validate a Hangzhou generation-2 template from the standalone
GHCR image, then update the site's default for new cloud computers.

Scope: 2 vCPU, 4096 MB memory, 10240 MB disk, Default QoS; retain 2 credits per
minute and existing idle/max lifetimes. Install storage clients during template
build. Do not initiate long-lived template prewarming or disrupt old instances.

Acceptance: verify cloud-side configuration, startup, direct OSS read/write,
image insertion into Word, PDF conversion, command execution and confirmed
instance destruction. Back up current site configuration before a conditional
update. Verify the site reads the new template and preserve a rollback record.
No real model invocation. Synthetic cloud tests remain bounded to 5 RMB.

Credentials stay outside Git and logs. Creation requires a classic GHCR pull PAT
as specified by Alibaba support. Template readiness alone is not acceptance.

## Progress on 2026-11-09

- Public source: `pengGgxp/zhiku-cloud-computer`, commit
  `4f8383561010a6f78d863aecd6a8f5353feadd59`; successful Actions run
  `42554856014`.
- Image: `ghcr.io/pengggxp/zhiku-cloud-computer` at immutable digest
  `sha256:9ab9a0a8dd493058967eb668f6d652ee00329f4a73592a1242fd60163542ea9b`.
  Single linux/amd64 Docker schema2 manifest; unpacked size about 1.60 GiB.
- Created standard Team `std-zhiku` (`2f22b3e8-b2c0-4244-b820-63f0339c9a2c`)
  and template `dk8taahy8ijoxqueebxc`, name `zhiku-docs-ghcr-4f83835`.
  Cloud API confirms CPU 2, memory 4096 MB, disk 10240 MB, generation 2.
  Storage-client installation requested; no long-lived prewarming requested.
- Build `ab1a5dc0-a776-4992-966e-7852263a2af2` accepted at
  `2026-11-09T03:24:44Z`. SDK logs report `template build accepted` and
  `image conversion completed`. At `03:49Z` both POP and Sandbox SDK still
  report `building`, with no further logs or failure reason.
- Separate standard-Team API key created and saved outside Git with restricted
  local file permissions. Credentials were not printed or committed.
- Server backup: `/data1/backups/cloud-template-20261109/database.dump` and
  `cloud-settings.before.json`; configuration update script is version guarded.
- Online configuration remains version 2, template `eidpiyb7xazavqt0n4go`,
  2 credits/minute, idle 600 seconds, maximum 3600 seconds. No cutover performed.
- No paid acceptance instance or real model call started. Word/PDF, OSS mount,
  dependency download and destruction acceptance remain unverified pending
  template readiness. Supplier billing has not been reconciled.

## Resume and rollback

Query the existing build before submitting or updating anything. Once ready,
run the synthetic test with a 180-second hard instance lifetime, verify direct
OSS file persistence and confirmed destruction, then recheck current settings
and apply the guarded update. Never switch based only on build readiness.

Retain the old Team, template and encrypted settings backup. Restore only the
settings row conditionally if rollback is required; do not restore the whole
database over newer user data. Existing sessions freeze their configuration and
credentials, so the new default must not change their lifecycle.
