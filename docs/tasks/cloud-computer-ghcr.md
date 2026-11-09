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
