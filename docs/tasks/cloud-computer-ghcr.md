# Hangzhou generation-2 GHCR image

## Scope
Build and publish a reproducible linux/amd64 document-processing image to GHCR,
retaining the init, SSH, time, FUSE and networking packages required by Alibaba.
No production template replacement, infrastructure purchase or paid cloud test.

## Acceptance
GitHub Actions must publish a Docker schema2 manifest, verify installed Python,
Node, document/image libraries, LibreOffice, PDF utilities and Chinese fonts, and
record the immutable digest and image size. Cloud startup remains a separate test.

## Template configuration
Use cn-hangzhou, generation=2 and the immutable image digest after verification.
The support ticket confirms GHCR works independently of the official overseas
template-publishing workflow. Supply registryConfig.authConfig with a GitHub
username and a classic PAT with read:packages through a secure configuration.
Do not commit credentials. Supported disk sizes per the ticket are 512, 10240
and 15360 MB; choose sufficient space for the unpacked image plus working data.
The package remains private unless explicitly changed; latest is a convenience
tag, not an immutable deployment reference.

## Build attempt: 2026-11-09
Commit b31f185 was pushed to codex/resource-library. Actions run 42553789035
was rejected before any runner steps started: GitHub reported failed account
payments or an insufficient spending limit. No image was built or published,
and no cloud template was created or changed. Local Docker Desktop also has
no running Linux engine. Resolve GitHub Billing and plans, then rerun the job;
manifest, installed-tool and cloud-startup acceptance remain pending.
