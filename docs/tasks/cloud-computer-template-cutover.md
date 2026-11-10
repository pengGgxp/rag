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

## ACR retry on 2026-11-09

- User authorized copying the existing image to the Hangzhou public personal ACR
  repository, then creating a new template. No rebuild or production cutover.
- Image repository: `crpi-wt6olxrbvra8q3wg.cn-hangzhou.personal.cr.aliyuncs.com/str1ct/zhiku-cloud-computer`.
- Standalone image repository commit `6d472f6` adds a manual registry-copy workflow.
  Actions run `42575585238` succeeded. Credentials are encrypted repository
  secrets; no credential values appear in source or verification artifacts.
- Tags `allinone-e8d9d3e` and `latest` retain digest
  `sha256:73af216889e100745d6637e98f786a1bfe6f8010328509b3f29d0eab48b5d1b6`.
  Raw source/target manifests match; Docker schema2 and linux/amd64 verified.
  A separate anonymous registry request verified the same digest and 46 layers.
- Template `vw3bbghjqjlgsoetjwea`, name `zhiku-allinone-acr-20261109`,
  submitted using the ACR public address with immutable digest. Request ID
  `01A1BF15-A47E-53F1-A7AA-BC55CAEC0F7C`. Failed with `IMAGE_NOT_EXIST`
  during create-function, despite anonymous registry digest verification succeeding.
  Generation 2, 2 vCPU, 4096 MB memory, 10240 MB disk, storage clients enabled.
- This verifies image publication and template submission only. Template readiness
  and real instance acceptance remain pending. Online default and prices unchanged.
- A second template uses the verified fixed tag instead of the digest address:
  `l811p6gvbqfbnuy74oqs`, name `zhiku-allinone-acr-tag-20261109`, tag
  `allinone-e8d9d3e`. Request ID `01A1BF16-38EC-5BAE-ACC9-CFF4396ECFE0`.
  Latest observed state: `building`. This tests address resolution; it does not
  establish that digest syntax caused the first failure.

## Resume and rollback

Query the existing build before submitting or updating anything. Once ready,
run the synthetic test with a 180-second hard instance lifetime, verify direct
OSS file persistence and confirmed destruction, then recheck current settings
and apply the guarded update. Never switch based only on build readiness.

Retain the old Team, template and encrypted settings backup. Restore only the
settings row conditionally if rollback is required; do not restore the whole
database over newer user data. Existing sessions freeze their configuration and
credentials, so the new default must not change their lifecycle.
